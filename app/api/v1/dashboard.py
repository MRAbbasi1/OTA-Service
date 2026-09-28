"""Dashboard and audit read endpoints.

Read-only by design: the dashboard observes, it does not act. Everything it shows
is derived from server-observed state, and the fleet summary points at the
categories that need a human rather than pretending to know an installation
result the protocol cannot report.

Audit records are append-only and reachable only with `audit:read`, so a viewer
role cannot see security history it has no reason to see.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session, get_settings
from app.api.v1.devices import device_read
from app.api.v1.schemas import (
    AttentionCategoryRead,
    AuditEventRead,
    DeviceHealthRead,
    FirmwareDistributionRow,
    FleetSummaryRead,
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


@audit_router.get("")
def list_audit_events(
    admin: AuditRead,
    action: str | None = None,
    actor: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """The audit trail, newest first. Read-only, and secret-free by construction."""
    rows, total = AuditService(session).list_events(
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [AuditEventRead.model_validate(row, from_attributes=True) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
