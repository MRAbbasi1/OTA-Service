from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

CODE = "bcs-controller-v1"
PLATFORM = "esp32-s3"
VERSION = "2.6.1"
FILENAME = "Controller.ino.bin"
BINARY = b"firmware-bytes" * 64
SIGNATURE = (
    "MEUCIA3pjuDY7q215q351W/FTH8qtm2EVWQVL51GRpU2YK1DAiEA+ptKGh8j1ziQ9+8KfTsJAsqMqUvDCWb"
    "FxKtu4ctf39E="
)

TYPE_PAYLOAD = {"code": CODE, "name": "Smart Controller", "platform": PLATFORM}

MANIFEST_BASE = str(get_settings().ota_manifest_base_url).rstrip("/")
FIRMWARE_BASE = str(get_settings().ota_firmware_base_url).rstrip("/")

pytestmark = pytest.mark.usefixtures("truncate_tables")


def expected_firmware_url(version: str = VERSION, filename: str = FILENAME) -> str:
    return f"{FIRMWARE_BASE}/firmware/{CODE}/{PLATFORM}/v{version}/{filename}"


def expected_manifest_url() -> str:
    return f"{MANIFEST_BASE}/api/v1/firmware/{CODE}/{PLATFORM}/manifest.json"


def manifest_payload(**overrides: object) -> bytes:
    document: dict[str, object] = {
        "version": VERSION,
        "url": expected_firmware_url(),
        "md5": hashlib.md5(BINARY).hexdigest(),
        "size": len(BINARY),
        "signature": SIGNATURE,
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":")).encode()


@pytest.fixture
def device_type(client: TestClient) -> dict:
    response = client.post("/api/v1/admin/device-types", json=TYPE_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


def upload(
    client: TestClient,
    device_type: dict,
    *,
    artifact_bytes: bytes = BINARY,
    artifact_name: str = FILENAME,
    manifest_bytes: bytes | None = None,
    version: str = VERSION,
):
    data = {"device_type_id": str(device_type["id"]), "version": version, "created_by": "tester"}
    files = {"artifact": (artifact_name, artifact_bytes, "application/octet-stream")}
    if manifest_bytes is not None:
        files["manifest"] = ("manifest.json", manifest_bytes, "application/json")
    return client.post("/api/v1/admin/firmware/releases", data=data, files=files)


class TestDeviceTypePathIdentity:
    def test_derived_urls_are_exposed(self, client: TestClient, device_type: dict) -> None:
        assert device_type["manifest_url"] == expected_manifest_url()
        assert device_type["firmware_url_template"] == (
            f"{FIRMWARE_BASE}/firmware/{{code}}/{{platform}}/v{{version}}/{{filename}}"
        )

    def test_device_registration_returns_the_provisioning_url(
        self, client: TestClient, device_type: dict
    ) -> None:
        response = client.post(
            "/api/v1/admin/devices",
            json={
                "device_type_id": device_type["id"],
                "serial_number": 10432,
                "raw_efuse_mac": "7C:9E:BD:12:34:56",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["expected_manifest_url"] == expected_manifest_url()

    def test_path_budget_reserves_room_for_the_version(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/device-types",
            json={**TYPE_PAYLOAD, "code": "bcs-controller-v1-abcd"},  # 22 + 8 > 29
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "device_type_path_budget_exceeded"

    def test_platform_pattern_is_enforced(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/device-types", json={**TYPE_PAYLOAD, "platform": "ESP32_S3"}
        )
        assert response.status_code == 422


class TestFirmwareUpload:
    def test_upload_and_read_back(self, client: TestClient, device_type: dict) -> None:
        response = upload(client, device_type, manifest_bytes=manifest_payload())
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["status"] == "validated"
        assert body["device_type_code"] == CODE
        assert body["platform"] == PLATFORM
        assert body["version"] == VERSION
        assert body["manifest_url"] == expected_manifest_url()
        assert body["firmware_url"] == expected_firmware_url()
        assert body["storage_prefix"] == f"firmware/{CODE}/{PLATFORM}/v{VERSION}"
        assert body["artifact"]["storage_key"] == (
            f"firmware/{CODE}/{PLATFORM}/v{VERSION}/{FILENAME}"
        )
        assert body["artifact"]["md5"] == hashlib.md5(BINARY).hexdigest()
        assert body["manifest"]["storage_key"] == (
            f"firmware/{CODE}/{PLATFORM}/v{VERSION}/manifest.json"
        )
        assert body["manifest"]["url"] == expected_firmware_url()
        # No production public key is available yet, so the signature is
        # structurally valid but not cryptographically verified.
        assert body["manifest"]["signature_verified"] is False

        detail = client.get(f"/api/v1/admin/firmware/releases/{body['id']}")
        assert detail.status_code == 200
        assert detail.json()["version"] == VERSION

        listing = client.get("/api/v1/admin/firmware/releases", params={"status": "validated"})
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["version"] == VERSION

    def test_served_manifest_bytes_are_the_uploaded_bytes(
        self, client: TestClient, device_type: dict
    ) -> None:
        uploaded = manifest_payload()
        release = upload(client, device_type, manifest_bytes=uploaded).json()
        served = client.get(f"/api/v1/admin/firmware/releases/{release['id']}/manifest")
        assert served.status_code == 200
        assert served.content == uploaded

    def test_upload_without_manifest_is_not_publishable(
        self, client: TestClient, device_type: dict
    ) -> None:
        body = upload(client, device_type).json()
        assert body["status"] == "uploaded"
        assert body["manifest"] is None

        assert (
            client.get(f"/api/v1/admin/firmware/releases/{body['id']}/manifest").status_code == 422
        )
        published = client.post(f"/api/v1/admin/firmware/releases/{body['id']}/publish")
        assert published.status_code == 409

    def test_publish_works_without_a_server_side_verifier(
        self, client: TestClient, device_type: dict
    ) -> None:
        """The pipeline signs; the device verifies. The platform only serves."""
        body = upload(client, device_type, manifest_bytes=manifest_payload()).json()
        response = client.post(f"/api/v1/admin/firmware/releases/{body['id']}/publish")
        assert response.status_code == 200, response.text
        published = response.json()
        assert published["status"] == "published"
        assert published["published_at"] is not None
        # Not independently verified on the server, which is the honest record.
        assert published["manifest"]["signature_verified"] is False

    @pytest.mark.parametrize(
        ("manifest_bytes", "expected_code"),
        [
            (b"{}", "manifest_invalid"),
            (manifest_payload(url="https://cdn.example.com/other.bin"), "manifest_url_mismatch"),
            (manifest_payload(version="2.6.2"), "manifest_version_mismatch"),
            (manifest_payload(md5="0" * 32), "manifest_md5_mismatch"),
            (manifest_payload(size=1), "manifest_size_mismatch"),
            (manifest_payload(signature=""), "missing_signature"),
            (manifest_payload(signature="not-base64!!"), "invalid_signature"),
        ],
    )
    def test_manifest_rejections(
        self, client: TestClient, device_type: dict, manifest_bytes: bytes, expected_code: str
    ) -> None:
        response = upload(client, device_type, manifest_bytes=manifest_bytes)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == expected_code

    def test_empty_artifact_is_rejected(self, client: TestClient, device_type: dict) -> None:
        response = upload(
            client, device_type, artifact_bytes=b"", manifest_bytes=manifest_payload()
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "artifact_empty"

    def test_traversal_filename_is_rejected(self, client: TestClient, device_type: dict) -> None:
        response = upload(client, device_type, artifact_name="../../etc/passwd")
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_filename"

    def test_duplicate_version_is_a_conflict(self, client: TestClient, device_type: dict) -> None:
        assert upload(client, device_type, manifest_bytes=manifest_payload()).status_code == 201
        duplicate = upload(client, device_type, manifest_bytes=manifest_payload())
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "version_exists"

    def test_unknown_release_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/admin/firmware/releases/9999").status_code == 404
