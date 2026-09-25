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
        listing = client.get("/api/v1/admin/device-types")
        assert listing.status_code == 200
        assert [row["id"] for row in listing.json()] == [device_type["id"]]

    def test_duplicate_code_conflict(self, client: TestClient, device_type: dict) -> None:
        response = client.post("/api/v1/admin/device-types", json=TYPE_PAYLOAD)
        assert response.status_code == 409

    def test_invalid_code_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/device-types", json={**TYPE_PAYLOAD, "code": "Bad Code!"}
        )
        assert response.status_code == 422


class TestDevices:
    def test_register_and_get(self, client: TestClient, device: dict) -> None:
        fetched = client.get(f"/api/v1/admin/devices/{device['id']}")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["serial_number"] == DEVICE_PAYLOAD["serial_number"]
        assert body["raw_efuse_mac"] == DEVICE_PAYLOAD["raw_efuse_mac"]
        assert body["status"] == "provisioned"

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

    def test_list_filter_and_pagination(self, client: TestClient, device: dict) -> None:
        listing = client.get(
            "/api/v1/admin/devices", params={"status": "provisioned", "page_size": 10}
        )
        assert listing.status_code == 200
        body = listing.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == device["id"]


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
