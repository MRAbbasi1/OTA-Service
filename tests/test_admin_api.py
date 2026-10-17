from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

pytestmark = pytest.mark.usefixtures("truncate_tables")

TYPE_PAYLOAD = {
    "code": "bcs-controller",
    "name": "Smart Controller",
    "platform": "esp32-s3",
}
DEVICE_PAYLOAD = {
    "device_type_id": 1,
    "serial_number": 10432,
    "raw_efuse_mac": "7C:9E:BD:12:34:56",
}


@contextmanager
def _session_scope(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    try:
        yield Session(bind=connection, expire_on_commit=False)
    finally:
        connection.close()


@pytest.fixture
def device_type(client: TestClient) -> dict:
    response = client.post("/api/v1/admin/device-types", json=TYPE_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def device(client: TestClient, device_type: dict) -> dict:
    payload = {**DEVICE_PAYLOAD, "device_type_id": device_type["id"]}
    response = client.post("/api/v1/admin/devices", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestDeviceTypes:
    def test_create_and_list(self, client: TestClient, device_type: dict) -> None:
        assert device_type["can_delete"] is True
        listing = client.get("/api/v1/admin/device-types")
        assert listing.status_code == 200
        assert [row["id"] for row in listing.json()["items"]] == [device_type["id"]]

    def test_list_supports_search_sort_filter_and_rejects_unknown_sort(
        self, client: TestClient, device_type: dict
    ) -> None:
        listing = client.get(
            "/api/v1/admin/device-types",
            params={"search": "controller", "is_active": True, "sort": "code", "order": "desc"},
        )
        assert listing.status_code == 200
        assert listing.json()["items"][0]["id"] == device_type["id"]
        assert client.get("/api/v1/admin/device-types", params={"sort": "id"}).status_code == 422

    def test_html_looking_description_remains_json_data(self, client: TestClient) -> None:
        untrusted_text = '<script>alert("xss")</script>'
        response = client.post(
            "/api/v1/admin/device-types",
            json={**TYPE_PAYLOAD, "description": untrusted_text},
        )

        assert response.status_code == 201
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["description"] == untrusted_text

    def test_duplicate_code_conflict(self, client: TestClient, device_type: dict) -> None:
        response = client.post("/api/v1/admin/device-types", json=TYPE_PAYLOAD)
        assert response.status_code == 409

    def test_invalid_code_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/device-types", json={**TYPE_PAYLOAD, "code": "Bad Code!"}
        )
        assert response.status_code == 422

    def test_update_activation_and_dependency_safe_delete(
        self, client: TestClient, device_type: dict
    ) -> None:
        updated = client.patch(
            f"/api/v1/admin/device-types/{device_type['id']}",
            json={"name": "Updated Controller", "description": "Production hardware"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Updated Controller"

        disabled = client.post(
            f"/api/v1/admin/device-types/{device_type['id']}/active",
            json={"is_active": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False
        registration = client.post(
            "/api/v1/admin/devices",
            json={**DEVICE_PAYLOAD, "device_type_id": device_type["id"]},
        )
        assert registration.status_code == 409
        assert registration.json()["detail"]["code"] == "device_type_inactive"

        assert client.delete(f"/api/v1/admin/device-types/{device_type['id']}").status_code == 200

    def test_identity_and_delete_lock_after_dependency(
        self, client: TestClient, device_type: dict, device: dict
    ) -> None:
        detail = client.get(f"/api/v1/admin/device-types/{device_type['id']}").json()
        assert detail["can_delete"] is False

        metadata = client.patch(
            f"/api/v1/admin/device-types/{device_type['id']}",
            json={
                "code": device_type["code"],
                "platform": device_type["platform"],
                "name": "Updated display name",
            },
        )
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["name"] == "Updated display name"

        identity = client.patch(
            f"/api/v1/admin/device-types/{device_type['id']}", json={"code": "changed-code"}
        )
        assert identity.status_code == 409
        assert identity.json()["detail"]["code"] == "device_type_identity_locked"
        deleted = client.delete(f"/api/v1/admin/device-types/{device_type['id']}")
        assert deleted.status_code == 409
        assert deleted.json()["detail"]["code"] == "device_type_has_dependencies"


class TestDevices:
    def test_register_and_get(self, client: TestClient, device: dict) -> None:
        fetched = client.get(f"/api/v1/admin/devices/{device['id']}")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["serial_number"] == DEVICE_PAYLOAD["serial_number"]
        assert body["raw_efuse_mac"] == DEVICE_PAYLOAD["raw_efuse_mac"]
        assert body["status"] == "provisioned"
        assert body["device_type_name"] == TYPE_PAYLOAD["name"]
        assert body["device_type_code"] == TYPE_PAYLOAD["code"]
        assert body["device_type_platform"] == TYPE_PAYLOAD["platform"]
        assert body["can_update_identity"] is True
        assert body["can_purge"] is True

    def test_registration_rejects_network_mac(self, client: TestClient, device_type: dict) -> None:
        response = client.post(
            "/api/v1/admin/devices",
            json={
                **DEVICE_PAYLOAD,
                "device_type_id": device_type["id"],
                "network_mac": "AA:BB:CC:DD:EE:FF",
            },
        )
        assert response.status_code == 422

    def test_duplicate_serial_conflict(
        self, client: TestClient, device: dict, device_type: dict
    ) -> None:
        response = client.post(
            "/api/v1/admin/devices",
            json={**DEVICE_PAYLOAD, "device_type_id": device_type["id"]},
        )
        assert response.status_code == 409

    def test_get_missing_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/admin/devices/9999").status_code == 404

    def test_missing_device_attempts_are_404_not_internal_error(self, client: TestClient) -> None:
        response = client.get("/api/v1/admin/devices/9999/update-attempts")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "device_not_found"

    def test_status_change_and_audit(self, client: TestClient, device: dict) -> None:
        response = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "active"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "active"

        ota = client.post(
            f"/api/v1/admin/devices/{device['id']}/ota-enabled",
            json={"ota_enabled": False},
        )
        assert ota.json()["ota_enabled"] is False

        version = client.post(
            f"/api/v1/admin/devices/{device['id']}/firmware-version",
            json={"version": "1.2.3"},
        )
        assert version.status_code == 200
        audit = client.get(
            "/api/v1/admin/audit-events",
            params={"resource_type": "device", "resource_id": device["id"]},
        ).json()["items"]
        details = {item["action"]: item["detail"] for item in audit}
        assert details["device_active"]["before"] == {"status": "provisioned"}
        assert details["device_active"]["after"] == {"status": "active"}
        assert details["device_ota_flag_changed"]["before"] == {"ota_enabled": True}
        assert details["device_ota_flag_changed"]["after"] == {"ota_enabled": False}
        assert details["device_firmware_version_asserted"]["before"] == {"version": None}
        assert details["device_firmware_version_asserted"]["after"] == {"version": "1.2.3"}
        assert all(item["detail"]["request_id"] for item in audit)

    def test_list_filter_and_pagination(self, client: TestClient, device: dict) -> None:
        listing = client.get(
            "/api/v1/admin/devices", params={"status": "provisioned", "page_size": 10}
        )
        assert listing.status_code == 200
        body = listing.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == device["id"]
        assert client.get("/api/v1/admin/devices", params={"sort": "unknown"}).status_code == 422

    def test_identity_edit_is_limited_to_unprovisioned_history_free_device(
        self, client: TestClient, device: dict
    ) -> None:
        updated = client.patch(
            f"/api/v1/admin/devices/{device['id']}",
            json={"serial_number": 20432, "raw_efuse_mac": "AA-BB-CC-DD-EE-01"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["serial_number"] == 20432
        assert updated.json()["raw_efuse_mac"] == "AA:BB:CC:DD:EE:01"

        client.post(f"/api/v1/admin/devices/{device['id']}/token/issue")
        locked = client.patch(
            f"/api/v1/admin/devices/{device['id']}", json={"serial_number": 30432}
        )
        assert locked.status_code == 409
        assert locked.json()["detail"]["code"] == "device_identity_locked"

    def test_retired_device_restores_disabled_before_reactivation(
        self, client: TestClient, device: dict
    ) -> None:
        retired = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "retired"}
        )
        assert retired.status_code == 200
        assert retired.json()["ota_enabled"] is False

        direct_activation = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "active"}
        )
        assert direct_activation.status_code == 409
        assert direct_activation.json()["detail"]["code"] == "invalid_device_transition"

        restored = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "disabled"}
        )
        assert restored.status_code == 200
        assert restored.json()["status"] == "disabled"
        assert restored.json()["ota_enabled"] is False

        activated = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "active"}
        )
        assert activated.status_code == 200

        audit = client.get("/api/v1/admin/audit-events").json()["items"]
        assert any(item["action"] == "device_restored" for item in audit)

    def test_unused_provisioned_device_can_be_purged(
        self, client: TestClient, device: dict
    ) -> None:
        assert device["can_purge"] is True
        response = client.delete(f"/api/v1/admin/devices/{device['id']}")

        assert response.status_code == 200
        assert response.json() == {"status": "deleted"}
        assert client.get(f"/api/v1/admin/devices/{device['id']}").status_code == 404

    def test_provisioned_device_and_its_tokens_can_be_purged(
        self, client: TestClient, device: dict, engine: Engine
    ) -> None:
        assert client.post(f"/api/v1/admin/devices/{device['id']}/token/issue").status_code == 201

        response = client.delete(f"/api/v1/admin/devices/{device['id']}")

        assert response.status_code == 200
        with engine.connect() as connection:
            token_count = connection.exec_driver_sql(
                "SELECT count(*) FROM device_tokens WHERE device_id = %s",
                (device["id"],),
            ).scalar_one()
        assert token_count == 0

    def test_database_delete_cascades_owned_tokens(
        self, client: TestClient, device: dict, engine: Engine
    ) -> None:
        assert client.post(f"/api/v1/admin/devices/{device['id']}/token/issue").status_code == 201

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM devices WHERE id = %s",
                (device["id"],),
            )
            token_count = connection.exec_driver_sql(
                "SELECT count(*) FROM device_tokens WHERE device_id = %s",
                (device["id"],),
            ).scalar_one()

        assert token_count == 0


class TestTokens:
    def test_issue_rotate_revoke_flow(self, client: TestClient, device: dict) -> None:
        issued = client.post(f"/api/v1/admin/devices/{device['id']}/token/issue")
        assert issued.status_code == 201
        token = issued.json()

        rotated = client.post(
            f"/api/v1/admin/devices/{device['id']}/token/rotate",
            params={"current_token_id": token["token_id"]},
        )
        assert rotated.status_code == 201
        assert rotated.json()["token"] != token["token"]

        revoked = client.post(
            f"/api/v1/admin/devices/{device['id']}/token/revoke",
            params={"token_id": rotated.json()["token_id"]},
        )
        assert revoked.status_code == 200

    def test_issue_for_missing_device_404(self, client: TestClient) -> None:
        assert client.post("/api/v1/admin/devices/9999/token/issue").status_code == 404

    def test_list_exposes_lifecycle_metadata_without_hash_or_plaintext(
        self, client: TestClient, device: dict
    ) -> None:
        issued = client.post(f"/api/v1/admin/devices/{device['id']}/token/issue").json()
        response = client.get(f"/api/v1/admin/devices/{device['id']}/tokens")
        assert response.status_code == 200
        assert response.json()[0]["id"] == issued["token_id"]
        assert response.json()[0]["is_active"] is True
        assert "token" not in response.json()[0]
        assert "token_hash" not in response.json()[0]


class TestAuditTrail:
    def test_device_actions_recorded(
        self, client: TestClient, device: dict, engine: Engine
    ) -> None:
        client.post(f"/api/v1/admin/devices/{device['id']}/status", json={"status": "active"})
        client.post(f"/api/v1/admin/devices/{device['id']}/token/issue")

        from sqlalchemy import select

        from app.db.models import AuditEvent

        with _session_scope(engine) as session:
            events = session.execute(select(AuditEvent)).scalars().all()
        actions = {event.action for event in events}
        assert "device_active" in actions
        assert "token_created" in actions
