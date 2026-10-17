"""Bounded, per-process operational evidence; never a raw log or durable archive."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.core.request_context import request_id_context
from app.services.audit_details import safe_request_id

EVENTS = frozenset(
    {
        "application_started",
        "communication_record_failed",
        "application_stopped",
        "unhandled_exception",
        "manifest_served",
        "manifest_no_offer",
        "device_auth_failed",
        "device_auth_forbidden",
        "device_rate_limited",
        "firmware_download_served",
        "firmware_stream_failed",
        "firmware_stream_size_mismatch",
        "firmware_stream_exceeded_expected_size",
        "firmware_download_completion_record_failed",
        "firmware_not_entitled",
        "firmware_release_not_found",
        "ota_transient_failure",
        "storage_read_failed",
        "storage_stream_open_failed",
        "stored_artifact_size_mismatch",
        "published_release_has_no_manifest",
        "published_release_has_no_artifact",
        "published_release_has_invalid_version",
        "firmware_blocked_by_policy",
        "policy_conflict_at_decision_time",
        "admin_login_failed",
        "admin_login_rate_limited",
        "admin_session_rejected",
        "admin_csrf_rejected",
        "admin_capability_denied",
        "admin_rate_limited",
    }
)
NUMERIC_FIELDS = frozenset(
    {
        "device_id",
        "device_serial",
        "release_id",
        "bytes",
        "expected",
        "actual",
        "emitted",
        "admin_id",
        "status_code",
        "duration_ms",
    }
)
CAPACITY = 500


class MonitoringBuffer(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.started_at = datetime.now(UTC)
        self.started_monotonic = time.monotonic()
        self._guard = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=CAPACITY)
        self._requests: deque[dict[str, Any]] = deque(maxlen=CAPACITY)
        self._sequence = 0
        self._capture_received = 0
        self._capture_failures = 0
        self._capture_last_write: datetime | None = None
        self._capture_last_failure: datetime | None = None

    def record_capture(self, event: str) -> None:
        with self._guard:
            now = datetime.now(UTC)
            if event == "failed":
                self._capture_failures += 1
                self._capture_last_failure = now
            else:
                self._capture_last_write = now
                if event == "received":
                    self._capture_received += 1

    def capture_status(self) -> dict[str, Any]:
        with self._guard:
            return {
                "received_in_process": self._capture_received,
                "write_failures_in_process": self._capture_failures,
                "last_persisted_at": self._capture_last_write,
                "last_failure_at": self._capture_last_failure,
                "persistence_degraded": self._capture_last_failure is not None
                and (
                    self._capture_last_write is None
                    or self._capture_last_failure > self._capture_last_write
                ),
            }

    def _append(
        self, event: str, level: str, request_id: object, fields: dict[str, object]
    ) -> None:
        with self._guard:
            self._sequence += 1
            self._events.append(
                {
                    "id": self._sequence,
                    "created_at": datetime.now(UTC),
                    "event": event,
                    "level": level,
                    "request_id": safe_request_id(request_id),
                    "fields": fields,
                }
            )

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("app.") or not isinstance(record.msg, str):
            return
        if record.msg not in EVENTS:
            return
        extra = getattr(record, "extra_fields", {})
        fields: dict[str, object] = {}
        if isinstance(extra, dict):
            fields = {
                key: value
                for key, value in extra.items()
                if key in NUMERIC_FIELDS
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            }
        level = (
            "ERROR"
            if record.levelno >= logging.ERROR
            else ("WARNING" if record.levelno >= logging.WARNING else "INFO")
        )
        self._append(
            record.msg,
            level,
            getattr(record, "request_id", None) or request_id_context.get(),
            fields,
        )

    def record_request(
        self,
        status: int,
        duration_ms: float,
        request_id: str,
        *,
        normal_no_offer: bool = False,
    ) -> None:
        with self._guard:
            self._requests.append(
                {
                    "created_at": datetime.now(UTC),
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )
        if status >= 400 and not normal_no_offer:
            self._append(
                "request_failed",
                "ERROR" if status >= 500 else "WARNING",
                request_id,
                {"status_code": status, "duration_ms": duration_ms},
            )

    def events(
        self,
        *,
        level: str | None,
        request_id: str | None,
        page: int,
        page_size: int,
        source: str | None = None,
    ) -> dict[str, Any]:
        with self._guard:
            rows = [
                dict(row)
                for row in reversed(self._events)
                if (level is None or row["level"] == level)
                and (request_id is None or row["request_id"] == request_id)
                and (
                    source != "system"
                    or row["event"]
                    in {
                        "application_started",
                        "application_stopped",
                        "unhandled_exception",
                        "storage_read_failed",
                        "storage_stream_open_failed",
                        "stored_artifact_size_mismatch",
                        "firmware_stream_failed",
                        "firmware_stream_size_mismatch",
                        "firmware_stream_exceeded_expected_size",
                        "firmware_download_completion_record_failed",
                        "communication_record_failed",
                        "ota_transient_failure",
                        "published_release_has_no_manifest",
                        "published_release_has_no_artifact",
                        "published_release_has_invalid_version",
                        "policy_conflict_at_decision_time",
                    }
                )
            ]
            start = (page - 1) * page_size
            return {
                "items": rows[start : start + page_size],
                "total": len(rows),
                "page": page,
                "page_size": page_size,
                "capacity": CAPACITY,
                "started_at": self.started_at,
            }

    def stats(self) -> dict[str, Any]:
        with self._guard:
            rows = list(self._requests)
        durations = sorted(row["duration_ms"] for row in rows)
        return {
            "observed_since": rows[0]["created_at"] if rows else self.started_at,
            "requests_observed": len(rows),
            "server_errors": sum(row["status"] >= 500 for row in rows),
            "p95_duration_ms": durations[math.ceil(len(durations) * 0.95) - 1]
            if durations
            else None,
            "uptime_seconds": int(time.monotonic() - self.started_monotonic),
            "capacity": CAPACITY,
        }
