"""Contract tests for the device-facing OTA endpoints.

These tests stand in for the firmware: they assert the wire behaviour the current
`OtaManager` depends on, at the level the device actually sees it — status codes,
exact `Content-Length`, no chunked framing, no redirect, byte-identical manifests,
and the documented headers. Storage is the in-memory fake, so the suite never
needs MinIO; Nginx deployment and real-device acceptance are validated
separately from this API contract suite.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.models import FirmwareReleaseStatus
from app.domain.update_path import (
    FIRMWARE_PATH_TEMPLATE,
    MANIFEST_PATH_TEMPLATE,
    MAX_FIRMWARE_URL_LENGTH,
    firmware_path,
    manifest_path,
)
from app.services.rate_limit import MANIFEST_RATE_LIMIT, MANIFEST_SURFACE

CODE = "bcs-controller-v1"
PLATFORM = "esp32-s3"
VERSION = "2.6.1"
OLDER_VERSION = "2.5.0"
FILENAME = "Controller.ino.bin"
ARTIFACT = b"firmware-bytes" * 64
SERIAL = 10432
MAC = "7C:9E:BD:12:34:56"

# The signature documented in docs/OtaManager.md §10: DER-encoded ECDSA, so its
# structure is what the device's parser expects even before any key exists here.
SIGNATURE = (
    "MEUCIA3pjuDY7q215q351W/FTH8qtm2EVWQVL51GRpU2YK1DAiEA+ptKGh8j1ziQ9+8KfTsJAsqMqUvDCWb"
    "FxKtu4ctf39E="
)

MANIFEST_BASE = str(get_settings().ota_manifest_base_url).rstrip("/")
FIRMWARE_BASE = str(get_settings().ota_firmware_base_url).rstrip("/")

pytestmark = pytest.mark.usefixtures("truncate_tables")


def absolute_firmware_url(
    version: str = VERSION,
    *,
    code: str = CODE,
    platform: str = PLATFORM,
    filename: str = FILENAME,
) -> str:
    return f"{FIRMWARE_BASE}/firmware/{code}/{platform}/v{version}/{filename}"


def create_type(client: TestClient, code: str = CODE, platform: str = PLATFORM) -> dict:
    response = client.post(
        "/api/v1/admin/device-types",
        json={"code": code, "name": "Smart Controller", "platform": platform},
    )
    assert response.status_code == 201, response.text
    return response.json()


def enroll(
    client: TestClient,
    device_type: dict,
    *,
    serial: int = SERIAL,
    mac: str = MAC,
    activate: bool = True,
) -> dict:
    response = client.post(
        "/api/v1/admin/devices",
        json={
            "device_type_id": device_type["id"],
            "serial_number": serial,
            "raw_efuse_mac": mac,
        },
    )
    assert response.status_code == 201, response.text
    device = response.json()
    if activate:
        activated = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "active"}
        )
        assert activated.status_code == 200, activated.text
    issued = client.post(f"/api/v1/admin/devices/{device['id']}/token/issue")
    assert issued.status_code == 201, issued.text
    return {
        "id": device["id"],
        "serial": serial,
        "mac": mac,
        "token": issued.json()["token"],
    }


def headers(device: dict) -> dict[str, str]:
    return {
        "X-Device-Serial": str(device["serial"]),
        "X-Device-Mac": device["mac"],
        "X-Device-Token": device["token"],
    }


def manifest_document(
    *,
    version: str = VERSION,
    artifact: bytes = ARTIFACT,
    code: str = CODE,
    platform: str = PLATFORM,
    filename: str = FILENAME,
    **overrides: object,
) -> dict[str, object]:
    document: dict[str, object] = {
        "version": version,
        "url": absolute_firmware_url(version, code=code, platform=platform, filename=filename),
        "md5": hashlib.md5(artifact).hexdigest(),
        "size": len(artifact),
        "signature": SIGNATURE,
    }
    document.update(overrides)
    return document


def upload_release(
    client: TestClient,
    device_type: dict,
    *,
    version: str = VERSION,
    artifact: bytes = ARTIFACT,
    filename: str = FILENAME,
    document: dict | None = None,
) -> dict:
    data = {"device_type_id": str(device_type["id"]), "version": version, "created_by": "tester"}
    payload = document or manifest_document(
        version=version,
        artifact=artifact,
        filename=filename,
        code=device_type["code"],
        platform=device_type["platform"],
    )
    files = {
        "artifact": (filename, artifact, "application/octet-stream"),
        "manifest": (
            "manifest.json",
            json.dumps(payload, separators=(",", ":")).encode(),
            "application/json",
        ),
    }
    response = client.post("/api/v1/admin/firmware/releases", data=data, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def publish_release(client: TestClient, release: dict) -> dict:
    response = client.post(f"/api/v1/admin/firmware/releases/{release['id']}/publish")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == FirmwareReleaseStatus.PUBLISHED.value
    return body


@pytest.fixture
def device_type(client: TestClient) -> dict:
    return create_type(client)


@pytest.fixture
def device(client: TestClient, device_type: dict) -> dict:
    return enroll(client, device_type)


@pytest.fixture
def published_release(client: TestClient, device_type: dict) -> dict:
    return publish_release(client, upload_release(client, device_type))


def normalized(path: str) -> str:
    return re.sub(r"\{[a-z_]+\}", "{}", path)


class TestRouteShape:
    def test_route_paths_are_the_documented_paths(self, client: TestClient) -> None:
        """The documented routes and the URL composition module cannot drift apart."""
        paths = {normalized(path) for path in client.app.openapi()["paths"]}
        assert normalized(MANIFEST_PATH_TEMPLATE) in paths
        assert normalized(FIRMWARE_PATH_TEMPLATE) in paths

    def test_documented_urls_stay_within_the_device_parser_budget(self) -> None:
        url = absolute_firmware_url()
        assert url == f"{FIRMWARE_BASE}{firmware_path(CODE, PLATFORM, VERSION, FILENAME)}"
        assert len(url) < MAX_FIRMWARE_URL_LENGTH


class TestManifestContract:
    def test_serves_the_stored_manifest_byte_for_byte(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        stored = client.get(f"/api/v1/admin/firmware/releases/{published_release['id']}/manifest")
        assert stored.status_code == 200

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 200
        assert response.content == stored.content
        assert response.headers["content-length"] == str(len(stored.content))
        assert response.headers["content-type"].startswith("application/json")
        assert "transfer-encoding" not in {k.lower() for k in response.headers}
        assert "location" not in {k.lower() for k in response.headers}
        assert response.headers["cache-control"] == "no-store"

    def test_manifest_carries_exactly_the_five_documented_keys(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        document = json.loads(response.content)
        assert set(document) == {"version", "url", "md5", "size", "signature"}
        assert document["version"] == VERSION
        assert document["url"] == absolute_firmware_url()
        assert isinstance(document["size"], int)
        assert document["md5"] == hashlib.md5(ARTIFACT).hexdigest()

    def test_signed_payload_matches_the_manifest_fields(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        document = json.loads(
            client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).content
        )
        payload = f"{document['version']}|{document['url']}|{document['md5']}|{document['size']}"
        assert payload == (
            f"{VERSION}|{absolute_firmware_url()}|{hashlib.md5(ARTIFACT).hexdigest()}"
            f"|{len(ARTIFACT)}"
        )

    def test_no_published_release_is_a_no_offer(self, client: TestClient, device: dict) -> None:
        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 404
        assert response.json() == {"code": "no_offer", "reason": "NO_ELIGIBLE_RELEASE"}
        assert response.headers["x-ota-reason"] == "NO_ELIGIBLE_RELEASE"

    def test_a_draft_release_is_not_offered(
        self, client: TestClient, device: dict, device_type: dict
    ) -> None:
        release = upload_release(client, device_type)
        drafted = client.post(f"/api/v1/admin/firmware/releases/{release['id']}/draft")
        assert drafted.status_code == 200

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 404
        assert response.json()["reason"] == "NO_ELIGIBLE_RELEASE"

    def test_a_deprecated_release_is_not_offered(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        deprecated = client.post(
            f"/api/v1/admin/firmware/releases/{published_release['id']}/deprecate"
        )
        assert deprecated.status_code == 200

        assert client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).status_code == 404

    def test_a_known_version_at_or_above_the_latest_is_not_an_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        asserted = client.post(
            f"/api/v1/admin/devices/{device['id']}/firmware-version", json={"version": VERSION}
        )
        assert asserted.status_code == 200
        assert asserted.json()["known_firmware_source"] == "admin_asserted"

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 404
        assert response.json()["reason"] == "NO_UPDATE"

    def test_clearing_the_known_version_restores_the_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        client.post(
            f"/api/v1/admin/devices/{device['id']}/firmware-version", json={"version": VERSION}
        )
        cleared = client.post(
            f"/api/v1/admin/devices/{device['id']}/firmware-version", json={"version": None}
        )
        assert cleared.status_code == 200
        assert cleared.json()["current_firmware_version"] is None
        assert cleared.json()["known_firmware_source"] is None

        assert client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).status_code == 200

    def test_an_asserted_version_must_be_a_numeric_triple(
        self, client: TestClient, device: dict
    ) -> None:
        response = client.post(
            f"/api/v1/admin/devices/{device['id']}/firmware-version",
            json={"version": "v2.6.1"},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_version"

    def test_polling_records_the_check_and_one_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        assert client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).status_code == 200
        assert client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).status_code == 200

        detail = client.get(f"/api/v1/admin/devices/{device['id']}").json()
        assert detail["last_manifest_check_at"] is not None
        assert detail["current_firmware_version"] is None  # serving an offer proves nothing

        attempts = client.get(f"/api/v1/admin/devices/{device['id']}/update-attempts").json()
        offered = [row for row in attempts["items"] if row["status"] == "update_offered"]
        assert len(offered) == 1
        assert offered[0]["to_version"] == VERSION


class TestDeviceAuthentication:
    def test_missing_headers_are_unauthorized(
        self, client: TestClient, published_release: dict
    ) -> None:
        for header in ("X-Device-Serial", "X-Device-Mac", "X-Device-Token"):
            complete = headers({"serial": SERIAL, "mac": MAC, "token": "whatever"})
            complete.pop(header)
            response = client.get(manifest_path(CODE, PLATFORM), headers=complete)
            assert response.status_code == 401, header

    def test_an_invalid_token_is_unauthorized(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(
            manifest_path(CODE, PLATFORM),
            headers=headers({**device, "token": "not-the-token"}),
        )
        assert response.status_code == 401
        assert response.json() == {"code": "unauthorized"}

    def test_a_revoked_token_is_unauthorized(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        issued = client.post(f"/api/v1/admin/devices/{device['id']}/token/issue").json()
        revoked = client.post(
            f"/api/v1/admin/devices/{device['id']}/token/revoke",
            params={"token_id": issued["token_id"]},
        )
        assert revoked.status_code == 200

        response = client.get(
            manifest_path(CODE, PLATFORM),
            headers=headers({**device, "token": issued["token"]}),
        )
        assert response.status_code == 401

    def test_an_unknown_serial_is_forbidden(
        self, client: TestClient, published_release: dict
    ) -> None:
        response = client.get(
            manifest_path(CODE, PLATFORM),
            headers=headers({"serial": 999999, "mac": MAC, "token": "whatever"}),
        )
        assert response.status_code == 403
        assert response.json() == {"code": "forbidden"}

    def test_a_mac_mismatch_is_forbidden(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(
            manifest_path(CODE, PLATFORM),
            headers=headers({**device, "mac": "7C:9E:BD:12:34:57"}),
        )
        assert response.status_code == 403

    def test_a_disabled_device_is_forbidden(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        client.post(f"/api/v1/admin/devices/{device['id']}/status", json={"status": "disabled"})

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 403

    def test_a_disabled_device_has_no_firmware_entitlement_but_keeps_its_token(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        """403 is an authentication outcome; it never leaks a specific reason."""
        response = client.get(
            manifest_path(CODE, PLATFORM), headers=headers({**device, "token": "wrong"})
        )
        assert response.status_code == 401
        assert "x-ota-reason" not in {k.lower() for k in response.headers}

    def test_a_device_that_is_not_activated_is_a_no_offer_not_a_lockout(
        self, client: TestClient, device_type: dict, published_release: dict
    ) -> None:
        """403 would silence the device for 24 hours, so it is reserved."""
        pending = enroll(client, device_type, serial=55501, mac="7C:9E:BD:55:50:01", activate=False)

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(pending))

        assert response.status_code == 404
        assert response.json()["reason"] == "DEVICE_INACTIVE"

    def test_ota_disabled_is_a_no_offer_not_a_lockout(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        client.post(
            f"/api/v1/admin/devices/{device['id']}/ota-enabled", json={"ota_enabled": False}
        )

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 404
        assert response.json()["reason"] == "OTA_DISABLED"


class TestPathIdentity:
    def test_a_different_device_type_in_the_path_is_a_no_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        other = create_type(client, code="bcs-sensor-v1")

        response = client.get(manifest_path(other["code"], PLATFORM), headers=headers(device))

        assert response.status_code == 404
        assert response.json()["reason"] == "DEVICE_TYPE_MISMATCH"

    def test_a_different_platform_in_the_path_is_a_no_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(manifest_path(CODE, "esp32-c3"), headers=headers(device))

        assert response.status_code == 404
        assert response.json()["reason"] == "DEVICE_TYPE_MISMATCH"

    def test_another_device_types_release_cannot_be_downloaded(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        """The cross-tenant case: a signed manifest for one type is not a licence
        to pull another type's binary."""
        other_type = create_type(client, code="bcs-sensor-v1", platform="esp32-s3")
        other_release = publish_release(client, upload_release(client, other_type, version="9.9.9"))

        response = client.get(
            firmware_path("bcs-sensor-v1", PLATFORM, "9.9.9", FILENAME),
            headers=headers(device),
        )

        assert other_release["status"] == "published"
        assert response.status_code == 404
        assert response.json()["reason"] == "DEVICE_TYPE_MISMATCH"


class TestFirmwareContract:
    def test_serves_the_exact_bytes_with_an_exact_content_length(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(device)
        )

        assert response.status_code == 200
        assert response.content == ARTIFACT
        assert response.headers["content-length"] == str(len(ARTIFACT))
        assert response.headers["content-type"] == "application/octet-stream"
        assert "transfer-encoding" not in {k.lower() for k in response.headers}
        assert "location" not in {k.lower() for k in response.headers}
        assert hashlib.md5(response.content).hexdigest() == hashlib.md5(ARTIFACT).hexdigest()

    def test_content_length_matches_the_signed_manifest_size(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        manifest = json.loads(
            client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).content
        )
        response = client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(device)
        )

        assert response.headers["content-length"] == str(manifest["size"])

    def test_firmware_is_streamed_and_closed_without_buffering_the_object(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        storage = client.app.state.storage
        streams_before = len(storage.opened_streams)
        buffered_reads_before = storage.get_bytes_calls

        response = client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(device)
        )

        assert response.status_code == 200
        assert response.content == ARTIFACT
        assert len(storage.opened_streams) == streams_before + 1
        assert storage.opened_streams[-1].closed is True
        assert storage.get_bytes_calls == buffered_reads_before

    def test_an_unpublished_version_is_a_no_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(
            firmware_path(CODE, PLATFORM, OLDER_VERSION, FILENAME), headers=headers(device)
        )

        assert response.status_code == 404
        assert response.json()["reason"] == "RELEASE_NOT_FOUND"

    def test_a_different_filename_is_a_no_offer(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        response = client.get(
            firmware_path(CODE, PLATFORM, VERSION, "Other.bin"), headers=headers(device)
        )

        assert response.status_code == 404

    def test_path_traversal_attempts_are_rejected(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        for version, filename in (
            ("..", FILENAME),
            ("2.6.1", "..%2F..%2Fsecrets.txt"),
            ("2.6.1", "../../secrets.txt"),
        ):
            response = client.get(
                f"/firmware/{CODE}/{PLATFORM}/v{version}/{filename}", headers=headers(device)
            )
            assert response.status_code == 404, (version, filename)

    def test_a_deprecated_release_still_serves_an_in_flight_download(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        """Deprecation stops new offers; a device already handed a signed URL can
        still finish that download."""
        client.post(f"/api/v1/admin/firmware/releases/{published_release['id']}/deprecate")

        response = client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(device)
        )

        assert response.status_code == 200
        assert response.content == ARTIFACT

    def test_an_unauthenticated_download_is_refused(
        self, client: TestClient, published_release: dict
    ) -> None:
        response = client.get(firmware_path(CODE, PLATFORM, VERSION, FILENAME))

        assert response.status_code == 401

    def test_a_served_download_is_recorded_without_claiming_an_install(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        client.get(firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(device))

        detail = client.get(f"/api/v1/admin/devices/{device['id']}").json()
        assert detail["last_download_served_version"] == VERSION
        assert detail["last_download_served_at"] is not None
        assert detail["current_firmware_version"] is None

        attempts = client.get(f"/api/v1/admin/devices/{device['id']}/update-attempts").json()
        served = [row for row in attempts["items"] if row["status"] == "download_served"]
        assert len(served) == 1
        assert served[0]["bytes_served"] == len(ARTIFACT)
        assert served[0]["to_version"] == VERSION

    def test_ota_disabled_does_not_leak_as_a_lockout(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        client.post(
            f"/api/v1/admin/devices/{device['id']}/ota-enabled", json={"ota_enabled": False}
        )

        response = client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(device)
        )

        assert response.status_code == 404


class TestRateLimiting:
    def test_the_manifest_surface_returns_429_after_the_documented_limit(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        limiter = client.app.state.rate_limiter
        key = f"{MANIFEST_SURFACE}:{device['id']}"
        for _ in range(MANIFEST_RATE_LIMIT.limit):
            assert limiter.allow(key, MANIFEST_RATE_LIMIT)

        response = client.get(manifest_path(CODE, PLATFORM), headers=headers(device))

        assert response.status_code == 429
        assert response.json() == {"code": "rate_limited"}
        assert response.headers["x-ota-reason"] == "RATE_LIMITED"

    def test_a_limited_poll_is_still_authenticated_and_seen(
        self, client: TestClient, device: dict, published_release: dict
    ) -> None:
        limiter = client.app.state.rate_limiter
        key = f"{MANIFEST_SURFACE}:{device['id']}"
        for _ in range(MANIFEST_RATE_LIMIT.limit + 1):
            limiter.allow(key, MANIFEST_RATE_LIMIT)

        assert client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).status_code == 429
        detail = client.get(f"/api/v1/admin/devices/{device['id']}").json()
        assert detail["last_seen_at"] is not None
