"""Dashboard and audit read endpoints.

Read-only by design: the dashboard observes, it does not act. Everything it shows
is derived from server-observed state, and the fleet summary points at the
categories that need a human rather than pretending to know an installation
result the protocol cannot report.

Audit records are append-only and reachable only with `audit:read`, so a viewer
role cannot see security history it has no reason to see.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session, get_settings
from app.api.v1.devices import device_read
from app.api.v1.evidence import EvidenceExport, export_evidence
from app.api.v1.schemas import (
    AttentionCategoryRead,
    AuditEventRead,
    AuditSummaryRead,
    DeviceHealthRead,
    FirmwareDistributionRow,
    FleetSummaryRead,
    Page,
    UpdateActivityRead,
    UpdateActivityRow,
)
from app.api.v1.security import AuditRead, DashboardRead
from app.core.config import Settings
from app.services.audit import AuditService
from app.services.dashboard import ATTENTION_EXPLANATIONS, AttentionCategory, DashboardService

dashboard_router = APIRouter(prefix="/api/v1/admin/dashboard", tags=["dashboard"])
audit_router = APIRouter(prefix="/api/v1/admin/audit-events", tags=["audit"])


@dashboard_router.get("/summary")
def fleet_summary(
    admin: DashboardRead,
    session: Session = Depends(get_session),
) -> FleetSummaryRead:
    summary = DashboardService(session).summary()
    return FleetSummaryRead(**summary.__dict__)


@dashboard_router.get("/firmware-distribution")
def firmware_distribution(
    admin: DashboardRead,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Known firmware versions per device type, with the unknown row included."""
    rows = DashboardService(session).firmware_distribution()
    return {
        "items": [FirmwareDistributionRow(**row.__dict__) for row in rows],
        "total": len(rows),
    }


@dashboard_router.get("/update-activity")
def update_activity(
    admin: DashboardRead,
    limit: int = Query(default=20, ge=1, le=200),
    session: Session = Depends(get_session),
) -> UpdateActivityRead:
    items, counts = DashboardService(session).update_activity(limit=limit)
    return UpdateActivityRead(
        items=[UpdateActivityRow(**item.__dict__) for item in items], **counts
    )


@dashboard_router.get("/device-health")
def device_health(
    admin: DashboardRead,
    session: Session = Depends(get_session),
) -> DeviceHealthRead:
    """Counts per attention category, each with the reason it is a concern."""
    service = DashboardService(session)
    counts = service.attention_counts()
    return DeviceHealthRead(
        categories=[
            AttentionCategoryRead(
                category=category.value,
                device_count=count,
                explanation=ATTENTION_EXPLANATIONS[category],
            )
            for category, count in counts.items()
        ],
        total_devices=service.summary().total_devices,
    )


@dashboard_router.get("/devices-needing-attention")
def devices_needing_attention(
    admin: DashboardRead,
    category: AttentionCategory,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    devices, total = DashboardService(session).devices_needing_attention(
        category, page=page, page_size=page_size
    )
    return {
        "items": [device_read(device, settings) for device in devices],
        "total": total,
        "page": page,
        "page_size": page_size,
        "category": category.value,
        "explanation": ATTENTION_EXPLANATIONS[category],
    }


@audit_router.get("/export")
def export_audit(
    admin: AuditRead,
    response: Response,
    format: Literal["csv", "json"] = "csv",
    action: str | None = None,
    actor: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    session: Session = Depends(get_session),
) -> EvidenceExport:
    response.headers["Cache-Control"] = "no-store"
    if any(value is not None and value.tzinfo is None for value in (since, until)):
        raise HTTPException(422, detail={"code": "audit_timezone_required"})
    if since and until and since > until:
        raise HTTPException(422, detail={"code": "audit_window_invalid"})
    rows, total = AuditService(session).list_events(
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=str(request_id) if request_id else None,
        since=since,
        until=until,
        page_size=1001,
    )
    if total > 1000:
        raise HTTPException(422, detail={"code": "export_limit_exceeded"})
    return export_evidence(
        [
            AuditEventRead.model_validate(row, from_attributes=True).model_dump(mode="json")
            for row in rows
        ],
        format=format,
        name="administrative-audit",
        snapshot=rows[0].id if rows else 0,
    )


@audit_router.get("/summary")
def audit_summary(
    admin: AuditRead,
    response: Response,
    session: Session = Depends(get_session),
) -> AuditSummaryRead:
    """Whole-trail rolling totals, independent of list filters and pagination."""
    response.headers["Cache-Control"] = "no-store"
    return AuditSummaryRead(**AuditService(session).summary())


@audit_router.get("")
def list_audit_events(
    admin: AuditRead,
    response: Response,
    action: str | None = None,
    actor: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    since: datetime | None = None,
    until: datetime | None = None,
    request_id: UUID | None = None,
) -> Page[AuditEventRead]:
    """The audit trail, newest first. Read-only, and secret-free by construction."""
    if any(value is not None and value.utcoffset() is None for value in (since, until)):
        raise HTTPException(422, detail={"code": "audit_timezone_required"})
    if since is not None and until is not None and since > until:
        raise HTTPException(422, detail={"code": "audit_window_invalid"})
    response.headers["Cache-Control"] = "no-store"
    rows, total = AuditService(session).list_events(
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        page=page,
        page_size=page_size,
        since=since,
        until=until,
        request_id=str(request_id) if request_id else None,
    )
    return Page[AuditEventRead](
        items=[AuditEventRead.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
