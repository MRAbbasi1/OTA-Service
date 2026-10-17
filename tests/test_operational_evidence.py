"""Operational failures must remain observable without inventing identity or success."""

from __future__ import annotations

import json
import logging
import sys

import pytest
from sqlalchemy import func, select, text

from app.api.v1.security import enforce_admin_rate_limit
from app.core.logging import JsonFormatter
from app.db.models import AdminUser, Device, DeviceToken, DeviceType, FirmwareRelease, UpdatePolicy
from app.services.audit import AuditService
from app.services.rate_limit import ADMIN_API_RATE_LIMIT, ADMIN_READ_RATE_LIMIT, InMemoryRateLimiter
from tests.test_communications import PATH
from tests.test_ota_api import (
    ARTIFACT,
    FILENAME,
    VERSION,
    create_type,
    enroll,
    headers,
    manifest_document,
    publish_release,
    upload_release,
)


def fail_audit(*args, **kwargs):
    raise RuntimeError("PRIVATE_AUDIT_FAILURE")


def test_exception_diagnostics_keep_locations_without_sensitive_prose():
    try:
        raise RuntimeError("DATABASE_PASSWORD_PRIVATE")
    except RuntimeError:
        record = logging.LogRecord(
            "app.test", logging.ERROR, __file__, 1, "unhandled_exception", (), sys.exc_info()
        )
    result = JsonFormatter().format(record)
    assert "DATABASE_PASSWORD_PRIVATE" not in result
    assert "RuntimeError" in result
    assert "test_exception_diagnostics" in result


def test_read_throttling_cannot_consume_administrative_write_budget(client):
    limiter = InMemoryRateLimiter()
    with client.app.state.session_factory() as session:
        admin = session.scalar(select(AdminUser))
        for _ in range(ADMIN_READ_RATE_LIMIT.limit):
            enforce_admin_rate_limit(admin, limiter, read=True)
        with pytest.raises(Exception) as limited:
            enforce_admin_rate_limit(admin, limiter, read=True)
        assert limited.value.status_code == 429
        assert limited.value.headers["Retry-After"] == "60"
        for _ in range(ADMIN_API_RATE_LIMIT.limit):
            enforce_admin_rate_limit(admin, limiter)
        with pytest.raises(Exception) as writes_limited:
            enforce_admin_rate_limit(admin, limiter)
        assert writes_limited.value.status_code == 429


def test_failed_upload_audit_rolls_back_release_and_uploaded_objects(
    client, fake_storage, monkeypatch
):
    kind = create_type(client)
    monkeypatch.setattr(AuditService, "record", fail_audit)
    response = client.post(
        "/api/v1/admin/firmware/releases",
        data={
            "device_type_id": str(kind["id"]),
            "version": VERSION,
        },
        files={
            "artifact": (FILENAME, ARTIFACT, "application/octet-stream"),
            "manifest": (
                "manifest.json",
                json.dumps(manifest_document()).encode(),
                "application/json",
            ),
        },
    )
    assert response.status_code == 500
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(FirmwareRelease)) == 0
    assert not fake_storage.objects


@pytest.mark.parametrize("entity", ["type", "device", "policy", "token", "status"])
def test_mutations_rollback_if_audit_cannot_be_recorded(client, monkeypatch, entity):
    kind = create_type(client)
    device = enroll(client, kind)
    monkeypatch.setattr(AuditService, "record", fail_audit)
    if entity == "type":
        result = client.post(
            "/api/v1/admin/device-types",
            json={
                "code": "must-rollback",
                "platform": "esp32",
                "name": "Rollback",
            },
        )
    elif entity == "device":
        result = client.post(
            "/api/v1/admin/devices",
            json={
                "device_type_id": kind["id"],
                "serial_number": 987654,
                "raw_efuse_mac": "02:00:00:44:55:66",
            },
        )
    elif entity == "policy":
        result = client.post(
            "/api/v1/admin/policies",
            json={
                "name": "Must rollback",
                "scope": "global",
                "policy_type": "disable",
            },
        )
    elif entity == "token":
        result = client.post(f"/api/v1/admin/devices/{device['id']}/token/issue")
    else:
        result = client.post(
            f"/api/v1/admin/devices/{device['id']}/status", json={"status": "disabled"}
        )
    assert result.status_code == 500
    assert "PRIVATE_AUDIT_FAILURE" not in result.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DeviceType)) == 1
        assert session.scalar(select(func.count()).select_from(Device)) == 1
        assert session.scalar(select(func.count()).select_from(UpdatePolicy)) == 0
        assert session.scalar(select(func.count()).select_from(DeviceToken)) == 1
        assert session.get(Device, device["id"]).status.value == "active"


def test_device_history_includes_credential_actions_without_secrets_or_other_devices(client):
    kind = create_type(client)
    device = enroll(client, kind)
    other = enroll(client, kind, serial=987654, mac="02:00:00:44:55:66")
    rows = client.get(
        "/api/v1/admin/audit-events",
        params={
            "resource_type": "device",
            "resource_id": device["id"],
        },
    ).json()["items"]
    tokens = [row for row in rows if row["resource_type"] == "device_token"]
    assert len(tokens) == 1
    assert tokens[0]["detail"]["device_id"] == device["id"]
    assert device["token"] not in json.dumps(rows)
    token_id = tokens[0]["resource_id"]
    denied = client.post(
        f"/api/v1/admin/devices/{other['id']}/token/revoke", params={"token_id": token_id}
    )
    assert denied.status_code == 404
    assert client.get(PATH, headers=headers(device)).status_code == 404


def test_resource_correlation_uses_recorded_decision_and_delivery(client):
    kind = create_type(client)
    device = enroll(client, kind)
    release = upload_release(client, kind)
    publish_release(client, release)
    policy = client.post(
        "/api/v1/admin/policies",
        json={
            "name": "Pinned",
            "scope": "device",
            "device_id": device["id"],
            "policy_type": "pin",
            "target_version": release["version"],
        },
    ).json()
    assert client.get(PATH, headers=headers(device)).status_code == 200
    for key, value in {
        "device_id": device["id"],
        "device_type_id": kind["id"],
        "release_id": release["id"],
        "policy_id": policy["id"],
    }.items():
        result = client.get("/api/v1/admin/communications", params={key: value, "period": "all"})
        assert result.status_code == 200
        assert result.json()["total"] == 1
        assert client.get("/api/v1/admin/communications", params={key: 999999}).json()["total"] == 0
    assert (
        client.post(
            f"/api/v1/admin/policies/{policy['id']}/active", json={"is_active": False}
        ).status_code
        == 200
    )
    # Historical policy attribution does not depend on today's active policy.
    assert (
        client.get("/api/v1/admin/communications", params={"policy_id": policy["id"]}).json()[
            "total"
        ]
        == 1
    )


def test_missing_history_schema_is_unavailable_not_empty_or_healthy(client):
    factory = client.app.state.session_factory
    with factory.begin() as session:
        session.execute(
            text("ALTER TABLE device_communications RENAME TO unavailable_history_test")
        )
    try:
        for suffix in ("", "/summary", "/export", "/1"):
            response = client.get("/api/v1/admin/communications" + suffix)
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "communication_history_unavailable"
            assert response.headers["cache-control"] == "no-store"
        health = client.get("/api/v1/admin/communications/diagnostics")
        assert health.status_code == 200
        assert health.headers["cache-control"] == "no-store"
        assert health.json()["status"] == "unavailable"
        assert health.json()["schema_available"] is False
        # History failure never rewrites the device wire response.
        assert client.get(PATH).status_code == 401
    finally:
        with factory.begin() as session:
            session.execute(
                text("ALTER TABLE unavailable_history_test RENAME TO device_communications")
            )
