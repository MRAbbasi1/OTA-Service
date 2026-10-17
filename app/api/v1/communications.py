"""Read-only device exchanges; admin audit and process health are separate resources."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session
from app.api.v1.evidence import EvidenceExport, RecordingHealth, export_evidence
from app.api.v1.security import LogsRead
from app.db.models import DeviceCommunication
from app.services.communications import CommunicationService, recording_health

router = APIRouter(prefix="/api/v1/admin/communications", tags=["device communications"])
Outcome = Literal[
    "received",
    "offered",
    "no_offer",
    "auth_rejected",
    "rate_limited",
    "server_error",
    "streaming",
    "download_served",
    "transfer_incomplete",
    "request_rejected",
]


class CommunicationRead(BaseModel):
    id: int
    request_id: str
    device_id: int | None
    claimed_serial: str | None
    kind: Literal["manifest", "firmware"]
    outcome: Outcome
    reason: str | None
    status_code: int | None
    duration_ms: float | None
    bytes_expected: int | None
    bytes_sent: int | None
    detail: dict[str, Any]
    created_at: datetime
    completed_at: datetime | None


class CommunicationPage(BaseModel):
    items: list[CommunicationRead]
    total: int
    page: int
    page_size: int
    snapshot: int
    since: datetime | None
    capture: RecordingHealth | None = None


class CommunicationSummary(BaseModel):
    since: datetime
    until: datetime
    total: int
    by_outcome: dict[str, int]
    last_received_at: datetime | None
    capture: RecordingHealth | None = None


class CommunicationFilters(BaseModel):
    serial: str | None = None
    device_id: int | None = None
    device_type_id: int | None = None
    release_id: int | None = None
    policy_id: int | None = None
    kind: Literal["manifest", "firmware"] | None = None
    outcome: Outcome | None = None
    since: datetime | None = None
    until: datetime | None = None
    request_id: UUID | None = None
    snapshot: int | None = None
    sort: Literal["newest", "oldest", "slowest"] = "newest"


def filters(
    serial: str | None = Query(default=None, pattern=r"^[0-9]{1,19}$"),
    device_id: int | None = Query(default=None, ge=1),
    device_type_id: int | None = Query(default=None, ge=1),
    release_id: int | None = Query(default=None, ge=1),
    policy_id: int | None = Query(default=None, ge=1),
    period: Literal["1h", "24h", "7d", "all"] | None = None,
    kind: Literal["manifest", "firmware"] | None = None,
    outcome: Outcome | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    request_id: UUID | None = None,
    snapshot: int | None = Query(default=None, ge=0),
    sort: Literal["newest", "oldest", "slowest"] = "newest",
) -> CommunicationFilters:
    if since is None and period in {"1h", "24h", "7d"}:
        since = datetime.now(UTC) - timedelta(hours={"1h": 1, "24h": 24, "7d": 168}[period])
    if any(value is not None and value.tzinfo is None for value in (since, until)):
        raise HTTPException(422, detail={"code": "communication_timezone_required"})
    if since is not None and until is not None and since > until:
        raise HTTPException(422, detail={"code": "communication_window_invalid"})
    return CommunicationFilters(
        serial=serial,
        device_id=device_id,
        device_type_id=device_type_id,
        release_id=release_id,
        policy_id=policy_id,
        kind=kind,
        outcome=outcome,
        since=since,
        until=until,
        request_id=request_id,
        snapshot=snapshot,
        sort=sort,
    )


Filters = Annotated[CommunicationFilters, Depends(filters)]


@contextmanager
def history_read(session: Session) -> Iterator[None]:
    """Translate actual read failures, without a second diagnostic DB session per poll."""
    try:
        yield
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(503, detail={"code": "communication_history_unavailable"}) from None


@router.get("/diagnostics")
def recording_diagnostics(admin: LogsRead, request: Request, response: Response) -> RecordingHealth:
    """Explicit read-only snapshot; never inserts a synthetic receipt or tests a write."""
    response.headers["Cache-Control"] = "no-store"
    return RecordingHealth(
        **recording_health(
            request.app.state.session_factory,
            request.app.state.monitoring.capture_status(),
            request.app.state.monitoring.started_at,
        )
    )


def filter_values(value: CommunicationFilters) -> dict[str, Any]:
    result = value.model_dump()
    result["request_id"] = str(value.request_id) if value.request_id else None
    return result


def read(row: DeviceCommunication) -> CommunicationRead:
    return CommunicationRead.model_validate(row, from_attributes=True)


@router.get("")
def communications(
    admin: LogsRead,
    response: Response,
    query: Filters,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(get_session),
) -> CommunicationPage:
    response.headers["Cache-Control"] = "no-store"
    with history_read(session):
        rows, total, snapshot = CommunicationService(session).list(
            **filter_values(query), page=page, page_size=page_size
        )
    return CommunicationPage(
        items=[read(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        snapshot=snapshot,
        since=query.since,
    )


@router.get("/summary")
def communication_summary(
    admin: LogsRead, response: Response, session: Session = Depends(get_session)
) -> CommunicationSummary:
    response.headers["Cache-Control"] = "no-store"
    with history_read(session):
        return CommunicationSummary(**CommunicationService(session).summary())


@router.get("/export")
def export_communications(
    admin: LogsRead,
    response: Response,
    query: Filters,
    format: Literal["csv", "json"] = "csv",
    session: Session = Depends(get_session),
) -> EvidenceExport:
    response.headers["Cache-Control"] = "no-store"
    with history_read(session):
        rows, total, snapshot = CommunicationService(session).list(
            **filter_values(query), page_size=1001
        )
    if total > 1000:
        raise HTTPException(422, detail={"code": "export_limit_exceeded"})
    return export_evidence(
        [read(row).model_dump(mode="json") for row in rows],
        format=format,
        name="device-communications",
        snapshot=snapshot,
    )


@router.get("/{communication_id}")
def communication_detail(
    communication_id: int,
    admin: LogsRead,
    response: Response,
    session: Session = Depends(get_session),
) -> CommunicationRead:
    response.headers["Cache-Control"] = "no-store"
    with history_read(session):
        row = session.get(DeviceCommunication, communication_id)
    if row is None:
        raise HTTPException(404, detail={"code": "communication_not_found"})
    return read(row)
