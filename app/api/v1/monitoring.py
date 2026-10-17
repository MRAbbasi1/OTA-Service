"""Authenticated monitoring, intentionally separate from private health probes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from app.api.v1.evidence import RecordingHealth
from app.api.v1.health import _database_ok, _storage_ok
from app.api.v1.security import LogsRead, MonitoringRead
from app.core.monitoring import MonitoringBuffer

router = APIRouter(prefix="/api/v1/admin/monitoring", tags=["monitoring"])


class MonitoringStatusRead(BaseModel):
    status: Literal["ok", "degraded"]
    checked_at: datetime
    app_version: str
    dependencies: dict[str, Literal["ok", "unavailable"]]
    uptime_seconds: int
    observed_since: datetime
    requests_observed: int
    server_errors: int
    p95_duration_ms: float | None
    capacity: int
    recording: RecordingHealth | None = None


class OperationalEventRead(BaseModel):
    id: int
    created_at: datetime
    event: str
    level: Literal["INFO", "WARNING", "ERROR"]
    request_id: str | None
    fields: dict[str, int | float]


class OperationalEventsRead(BaseModel):
    items: list[OperationalEventRead]
    total: int
    page: int
    page_size: int
    capacity: int
    started_at: datetime


@router.get("/status")
def monitoring_status(
    admin: MonitoringRead,
    request: Request,
    response: Response,
) -> MonitoringStatusRead:
    response.headers["Cache-Control"] = "no-store"
    database_ok = _database_ok(request.app.state.database)
    storage_ok = _storage_ok(request.app.state.storage)
    buffer: MonitoringBuffer = request.app.state.monitoring
    return MonitoringStatusRead(
        status="ok" if database_ok and storage_ok else "degraded",
        checked_at=datetime.now(UTC),
        app_version=request.app.state.settings.app_version,
        dependencies={
            "database": "ok" if database_ok else "unavailable",
            "object_storage": "ok" if storage_ok else "unavailable",
        },
        **buffer.stats(),
    )


@router.get("/events")
def operational_events(
    admin: LogsRead,
    request: Request,
    response: Response,
    level: Literal["INFO", "WARNING", "ERROR"] | None = None,
    request_id: UUID | None = None,
    source: Literal["system"] | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> OperationalEventsRead:
    response.headers["Cache-Control"] = "no-store"
    buffer: MonitoringBuffer = request.app.state.monitoring
    return OperationalEventsRead(
        **buffer.events(
            level=level,
            request_id=str(request_id) if request_id else None,
            page=page,
            page_size=page_size,
            source=source,
        )
    )
