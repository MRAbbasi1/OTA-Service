from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.monitoring import CAPACITY, MonitoringBuffer
from app.db.models import AuditEvent
from app.services.audit import AuditService
from app.services.audit_details import safe_detail
from tests.conftest import create_admin_account, login


def test_audit_details_are_allowlisted_recursively() -> None:
    cleaned = safe_detail(
        {
            "password": "DO_NOT_LEAK",
            "token": "DO_NOT_LEAK",
            "storage_key": "DO_NOT_LEAK",
            "before": {"status": "active", "token": "DO_NOT_LEAK"},
            "after": {"status": "disabled", "connection_url": "DO_NOT_LEAK"},
            "changed": ["status", "password", "token"],
            "request_id": "not-a-uuid",
        }
    )
    assert cleaned == {
        "before": {"status": "active"},
        "after": {"status": "disabled"},
        "changed": ["status"],
    }
    assert "DO_NOT_LEAK" not in str(cleaned)


def test_operational_buffer_is_bounded_and_never_copies_raw_logs() -> None:
    buffer = MonitoringBuffer()
    request_id = str(uuid.uuid4())
    for _ in range(CAPACITY + 4):
        record = logging.LogRecord(
            "app.test", logging.ERROR, "", 1, "storage_read_failed", (), None
        )
        record.request_id = request_id
        record.extra_fields = {
            "device_id": 23,
            "storage_key": "DO_NOT_LEAK",
            "token": "DO_NOT_LEAK",
            "address": "DO_NOT_LEAK",
            "exception": "DO_NOT_LEAK",
        }
        buffer.emit(record)
    result = buffer.events(level=None, request_id=None, page=1, page_size=5)
    assert result["total"] == CAPACITY
    assert len(result["items"]) == 5
    assert result["items"][0]["id"] == CAPACITY + 4
    assert result["items"][0]["request_id"] == request_id
    assert result["items"][0]["fields"] == {"device_id": 23}
    assert "DO_NOT_LEAK" not in str(result)
    buffer.emit(logging.LogRecord("app.test", logging.ERROR, "", 1, "secret raw log", (), None))
    assert buffer.events(level="INFO", request_id=None, page=1, page_size=5)["total"] == 0


def test_sample_metrics_are_measured_and_empty_latency_is_unknown() -> None:
    buffer = MonitoringBuffer()
    assert buffer.stats()["p95_duration_ms"] is None
    for status, duration in [(200, 2.0), (500, 60.0), (404, 9.0)]:
        buffer.record_request(status, duration, str(uuid.uuid4()))
    stats = buffer.stats()
    assert stats["requests_observed"] == 3
    assert stats["server_errors"] == 1
    assert stats["p95_duration_ms"] == 60.0


@pytest.mark.usefixtures("truncate_tables")
class TestMonitoringApi:
    def test_authentication_and_distinct_permissions(self, anonymous_client: TestClient) -> None:
        for path in ("/status", "/events"):
            assert anonymous_client.get("/api/v1/admin/monitoring" + path).status_code == 401
        create_admin_account(
            anonymous_client.app, "fleet@test.example", "fleet-test-password", "fleet_manager"
        )
        login(anonymous_client, "fleet@test.example", "fleet-test-password")
        for path, capability in (("/status", "monitoring:read"), ("/events", "logs:read")):
            response = anonymous_client.get("/api/v1/admin/monitoring" + path)
            assert response.status_code == 403
            assert response.json()["detail"]["required"] == capability

    def test_status_never_leaks_storage_failure(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail() -> bool:
            raise RuntimeError("DO_NOT_LEAK connection credentials")

        monkeypatch.setattr(client.app.state.storage, "is_healthy", fail)
        response = client.get("/api/v1/admin/monitoring/status")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["status"] == "degraded"
        assert response.json()["dependencies"]["object_storage"] == "unavailable"
        assert "DO_NOT_LEAK" not in response.text
        assert "endpoint" not in response.text

    def test_event_filters_and_pagination(self, client: TestClient) -> None:
        request_id = str(uuid.uuid4())
        client.get("/missing", headers={"X-Request-ID": request_id})
        response = client.get(
            "/api/v1/admin/monitoring/events",
            params={"request_id": request_id, "level": "WARNING", "page_size": 1},
        )
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["items"][0]["event"] == "request_failed"
        assert response.json()["items"][0]["fields"]["status_code"] == 404
        assert (
            client.get(
                "/api/v1/admin/monitoring/events", params={"request_id": "unsafe"}
            ).status_code
            == 422
        )
        assert (
            client.get("/api/v1/admin/monitoring/events", params={"page_size": 501}).status_code
            == 422
        )

    def test_audit_window_correlation_and_historical_redaction(
        self,
        client: TestClient,
        engine: Engine,
    ) -> None:
        request_id = str(uuid.uuid4())
        response = client.post(
            "/api/v1/admin/device-types",
            json={
                "code": "audit-test",
                "name": "Audit test",
                "platform": "esp32",
            },
            headers={"X-Request-ID": request_id},
        )
        assert response.status_code == 201
        rows = client.get("/api/v1/admin/audit-events", params={"request_id": request_id}).json()[
            "items"
        ]
        assert len(rows) == 1
        assert rows[0]["actor"]
        assert rows[0]["detail"]["request_id"] == request_id
        with Session(engine) as session:
            event = session.scalar(select(AuditEvent).where(AuditEvent.id == rows[0]["id"]))
            assert event is not None
            event.detail = {
                "token": "DO_NOT_LEAK",
                "storage_key": "DO_NOT_LEAK",
                "before": {"status": "active", "password": "DO_NOT_LEAK"},
            }
            session.commit()
        listing = client.get("/api/v1/admin/audit-events")
        assert "DO_NOT_LEAK" not in listing.text
        assert (
            client.get(
                "/api/v1/admin/audit-events", params={"since": "2026-01-01T00:00:00"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/admin/audit-events",
                params={
                    "since": "2026-02-01T00:00:00Z",
                    "until": "2026-01-01T00:00:00Z",
                },
            ).status_code
            == 422
        )
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        assert (
            client.get("/api/v1/admin/audit-events", params={"since": future}).json()["total"] == 0
        )

    def test_summary_counts_whole_trail_and_fills_empty_hours(
        self,
        client: TestClient,
        engine: Engine,
    ) -> None:
        with Session(engine) as session:
            for _ in range(55):
                AuditService(session).record(
                    "test@example.test", "admin_login_failed", "admin_user"
                )
            session.commit()
            session.execute(text("SET LOCAL TIME ZONE 'Asia/Tehran'"))
            local_summary = AuditService(session).summary()
            assert (
                sum(bucket["count"] for bucket in local_summary["buckets"])
                == local_summary["total"]
            )
        summary = client.get("/api/v1/admin/audit-events/summary").json()
        assert summary["failed_logins"] == 55
        assert summary["total"] >= 56
        assert sum(bucket["count"] for bucket in summary["buckets"]) == summary["total"]
        assert len(summary["buckets"]) == 25
