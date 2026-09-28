"""Dashboard and audit read endpoints.

The dashboard is where a wrong number becomes a wrong decision, so these tests
check that every figure is derived from server-observed state and that the
"needs attention" categories mean what their explanation says.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Device, UpdateAttempt, UpdateAttemptStatus
from app.domain.update_path import firmware_path, manifest_path
from tests.conftest import SUPER_ADMIN_EMAIL, create_admin_account, login
from tests.test_ota_api import (
    CODE,
    FILENAME,
    PLATFORM,
    VERSION,
    create_type,
    enroll,
    headers,
    publish_release,
    upload_release,
)

pytestmark = pytest.mark.usefixtures("truncate_tables")

NOW = datetime.now(UTC)


@pytest.fixture
def device_type(client: TestClient) -> dict:
    return create_type(client)


@pytest.fixture
def published_device(client: TestClient, device_type: dict) -> dict:
    device = enroll(client, device_type)
    publish_release(client, upload_release(client, device_type))
    return device


def session_scope(engine: Engine) -> Session:
    return Session(bind=engine, expire_on_commit=False)


class TestSummary:
    def test_counts_devices_by_state_and_flags(self, client: TestClient, device_type: dict) -> None:
        enroll(client, device_type, serial=1, mac="7C:9E:BD:00:00:01")
        enroll(client, device_type, serial=2, mac="7C:9E:BD:00:00:02")
        pending = enroll(client, device_type, serial=3, mac="7C:9E:BD:00:00:03", activate=False)
        disabled = enroll(client, device_type, serial=4, mac="7C:9E:BD:00:00:04")
        client.post(f"/api/v1/admin/devices/{disabled['id']}/status", json={"status": "disabled"})
        client.post(
            f"/api/v1/admin/devices/{pending['id']}/ota-enabled", json={"ota_enabled": False}
        )

        summary = client.get("/api/v1/admin/dashboard/summary").json()

        assert summary["total_devices"] == 4
        assert summary["active_devices"] == 2
        assert summary["provisioned_devices"] == 1
        assert summary["disabled_devices"] == 1
        assert summary["retired_devices"] == 0
        assert summary["ota_disabled_devices"] == 1
        assert summary["total_device_types"] == 1
        assert summary["generated_at"] is not None

    def test_counts_releases_policies_and_a_poll(
        self, client: TestClient, device_type: dict
    ) -> None:
        device = enroll(client, device_type)
        publish_release(client, upload_release(client, device_type))
        assert client.get(manifest_path(CODE, PLATFORM), headers=headers(device)).status_code == 200
        # Policy comes last, so the poll above is not itself blocked by it.
        client.post(
            "/api/v1/admin/policies",
            json={"name": "freeze", "scope": "global", "policy_type": "disable"},
        )

        summary = client.get("/api/v1/admin/dashboard/summary").json()

        assert summary["published_releases"] == 1
        assert summary["active_policies"] == 1
        assert summary["checks_last_24h"] == 1
        assert summary["offers_last_24h"] == 1
        assert summary["downloads_last_24h"] == 0

    def test_it_needs_the_dashboard_capability(self, anonymous_client: TestClient) -> None:
        create_admin_account(
            anonymous_client.app, "v@ota-service.test", "viewer-password-123", "viewer"
        )
        login(anonymous_client, "v@ota-service.test", "viewer-password-123")

        assert anonymous_client.get("/api/v1/admin/dashboard/summary").status_code == 200

    def test_it_is_refused_without_a_session(self, anonymous_client: TestClient) -> None:
        assert anonymous_client.get("/api/v1/admin/dashboard/summary").status_code == 401


class TestFirmwareDistribution:
    def test_an_unasserted_version_is_a_row_not_an_omission(
        self, client: TestClient, published_device: dict, device_type: dict
    ) -> None:
        asserted = enroll(client, device_type, serial=2, mac="7C:9E:BD:00:00:02")
        client.post(
            f"/api/v1/admin/devices/{asserted['id']}/firmware-version",
            json={"version": VERSION},
        )

        rows = client.get("/api/v1/admin/dashboard/firmware-distribution").json()["items"]

        by_version = {row["version"]: row for row in rows}
        assert set(by_version) == {None, VERSION}
        assert by_version[None]["device_count"] == 1
        assert by_version[None]["version_source"] is None
        assert by_version[VERSION]["device_count"] == 1
        assert by_version[VERSION]["version_source"] == "admin_asserted"
        assert by_version[VERSION]["device_type_code"] == CODE

    def test_devices_of_different_types_are_counted_separately(
        self, client: TestClient, device_type: dict
    ) -> None:
        other = create_type(client, code="bcs-sensor-v1")
        enroll(client, device_type, serial=1, mac="7C:9E:BD:00:00:01")
        enroll(client, other, serial=2, mac="7C:9E:BD:00:00:02")

        rows = client.get("/api/v1/admin/dashboard/firmware-distribution").json()["items"]

        assert {row["device_type_code"] for row in rows} == {CODE, "bcs-sensor-v1"}


class TestUpdateActivity:
    def test_recent_events_are_listed_newest_first(
        self, client: TestClient, published_device: dict, device_type: dict
    ) -> None:
        client.get(manifest_path(CODE, PLATFORM), headers=headers(published_device))
        client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(published_device)
        )

        body = client.get("/api/v1/admin/dashboard/update-activity").json()

        assert [row["status"] for row in body["items"]] == ["download_served", "update_offered"]
        assert body["items"][0]["device_serial"] == published_device["serial"]
        assert body["items"][0]["to_version"] == VERSION
        assert body["items"][0]["bytes_served"] > 0
        assert body["offers_last_24h"] == 1
        assert body["downloads_last_24h"] == 1
        assert body["offers_last_7d"] == 1

    def test_a_limited_page_is_honoured(self, client: TestClient, published_device: dict) -> None:
        client.get(manifest_path(CODE, PLATFORM), headers=headers(published_device))

        body = client.get("/api/v1/admin/dashboard/update-activity", params={"limit": 1}).json()

        assert len(body["items"]) == 1


class TestDeviceHealth:
    def test_a_registered_device_that_never_authenticated_is_flagged(
        self, client: TestClient, device_type: dict, engine: Engine
    ) -> None:
        device = enroll(client, device_type)
        with engine.begin() as connection:
            connection.execute(
                Device.__table__.update()
                .where(Device.id == device["id"])
                .values(registered_at=NOW - timedelta(days=3))
            )

        health = client.get("/api/v1/admin/dashboard/device-health").json()

        counts = {row["category"]: row["device_count"] for row in health["categories"]}
        assert counts["NEVER_SEEN"] == 1
        assert counts["STALE_CHECK"] == 1  # active, and it has never polled
        assert counts["DOWNLOAD_UNCONFIRMED"] == 0
        assert health["total_devices"] == 1

    def test_each_category_explains_itself(self, client: TestClient, device_type: dict) -> None:
        enroll(client, device_type)

        health = client.get("/api/v1/admin/dashboard/device-health").json()

        for row in health["categories"]:
            assert row["explanation"]
        stale = next(row for row in health["categories"] if row["category"] == "STALE_CHECK")
        assert "not polling" in stale["explanation"]

    def test_a_served_download_that_never_became_the_known_version(
        self, client: TestClient, published_device: dict, engine: Engine
    ) -> None:
        """The honest form of "the update did not take"."""
        client.get(manifest_path(CODE, PLATFORM), headers=headers(published_device))
        client.get(
            firmware_path(CODE, PLATFORM, VERSION, FILENAME), headers=headers(published_device)
        )
        with engine.begin() as connection:
            connection.execute(
                Device.__table__.update()
                .where(Device.id == published_device["id"])
                .values(
                    last_download_served_at=NOW - timedelta(days=3),
                    last_manifest_check_at=NOW,
                )
            )

        counts = {
            row["category"]: row["device_count"]
            for row in client.get("/api/v1/admin/dashboard/device-health").json()["categories"]
        }

        assert counts["DOWNLOAD_UNCONFIRMED"] == 1
        assert counts["NEVER_SEEN"] == 0  # it has authenticated

    def test_the_attention_total_counts_a_device_once(
        self, client: TestClient, device_type: dict, engine: Engine
    ) -> None:
        device = enroll(client, device_type)
        with engine.begin() as connection:
            connection.execute(
                Device.__table__.update()
                .where(Device.id == device["id"])
                .values(
                    registered_at=NOW - timedelta(days=3),
                    last_download_served_version=VERSION,
                    last_download_served_at=NOW - timedelta(days=3),
                )
            )
            connection.execute(
                UpdateAttempt.__table__.insert().values(
                    device_id=device["id"],
                    status=UpdateAttemptStatus.DOWNLOAD_SERVED.value,
                    to_version=VERSION,
                    bytes_served=10,
                )
            )

        summary = client.get("/api/v1/admin/dashboard/summary").json()

        # Matches all three categories but is one device needing attention.
        assert summary["devices_needing_attention"] == 1

    def test_the_listing_narrows_to_one_category_and_paginates(
        self, client: TestClient, device_type: dict, engine: Engine
    ) -> None:
        for index in range(3):
            device = enroll(
                client, device_type, serial=100 + index, mac=f"7C:9E:BD:00:01:{index:02X}"
            )
            with engine.begin() as connection:
                connection.execute(
                    Device.__table__.update()
                    .where(Device.id == device["id"])
                    .values(last_manifest_check_at=None, last_seen_at=None)
                )

        body = client.get(
            "/api/v1/admin/dashboard/devices-needing-attention",
            params={"category": "STALE_CHECK", "page_size": 2},
        ).json()

        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["category"] == "STALE_CHECK"
        assert body["items"][0]["serial_number"] == 100


class TestAuditEvents:
    def test_the_trail_is_readable_newest_first_with_the_actor(self, client: TestClient) -> None:
        client.post(
            "/api/v1/admin/device-types",
            json={"code": "bcs-controller", "name": "Controller", "platform": "esp32-s3"},
        )

        body = client.get("/api/v1/admin/audit-events").json()

        actions = [row["action"] for row in body["items"]]
        assert actions[0] == "device_type_created"
        assert actions[-1] == "admin_login"
        assert body["items"][0]["actor"] == SUPER_ADMIN_EMAIL

    def test_filters_narrow_the_trail(self, client: TestClient, device_type: dict) -> None:
        device = enroll(client, device_type)
        client.post(f"/api/v1/admin/devices/{device['id']}/token/issue")

        by_resource = client.get(
            "/api/v1/admin/audit-events",
            params={"resource_type": "device_token"},
        ).json()
        by_actor = client.get(
            "/api/v1/admin/audit-events", params={"actor": "nobody@ota-service.test"}
        ).json()

        assert {row["action"] for row in by_resource["items"]} == {"token_created"}
        assert by_resource["total"] >= 1
        assert by_actor["total"] == 0

    def test_reading_the_trail_needs_the_audit_capability(
        self, anonymous_client: TestClient
    ) -> None:
        create_admin_account(
            anonymous_client.app, "v@ota-service.test", "viewer-password-123", "viewer"
        )
        login(anonymous_client, "v@ota-service.test", "viewer-password-123")

        response = anonymous_client.get("/api/v1/admin/audit-events")

        assert response.status_code == 403
        assert response.json()["detail"]["required"] == "audit:read"
