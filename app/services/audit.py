from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.request_context import request_id_context
from app.db.models import AuditEvent
from app.services.audit_details import safe_detail, safe_request_id

logger = logging.getLogger(__name__)


class AuditService:
    """Append-only audit trail; never stores secrets.

    Reviewed fields are allowlisted on write and read. Secrets and arbitrary
    prose are never copied into the operator-facing detail.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str | int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        cleaned = safe_detail(detail) or {}
        request_id = safe_request_id(request_id_context.get())
        if request_id is not None:
            cleaned["request_id"] = request_id
        event = AuditEvent(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            detail=cleaned or None,
        )
        self._session.add(event)
        logger.info(
            "audit_event",
            extra={"extra_fields": {"action": action, "resource_type": resource_type}},
        )

    def list_events(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
        since: datetime | None = None,
        until: datetime | None = None,
        request_id: str | None = None,
    ) -> tuple[list[AuditEvent], int]:
        """Read the trail, newest first. There is no update or delete."""
        conditions = []
        if action:
            conditions.append(AuditEvent.action == action)
        if actor:
            conditions.append(AuditEvent.actor == actor)
        if resource_type == "device" and resource_id:
            conditions.append(
                or_(
                    and_(
                        AuditEvent.resource_type == "device", AuditEvent.resource_id == resource_id
                    ),
                    and_(
                        AuditEvent.resource_type == "device_token",
                        AuditEvent.detail["device_id"].as_string() == resource_id,
                    ),
                )
            )
        else:
            if resource_type:
                conditions.append(AuditEvent.resource_type == resource_type)
            if resource_id:
                conditions.append(AuditEvent.resource_id == resource_id)
        if since is not None:
            conditions.append(AuditEvent.created_at >= since)
        if until is not None:
            conditions.append(AuditEvent.created_at <= until)
        if request_id is not None:
            conditions.append(AuditEvent.detail["request_id"].as_string() == request_id)
        stmt = select(AuditEvent)
        if conditions:
            stmt = stmt.where(*conditions)
        total = int(
            self._session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        )
        rows = self._session.execute(
            stmt.order_by(AuditEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
        return list(rows), total

    def summary(self) -> dict[str, Any]:
        until = datetime.now(UTC)
        since = until - timedelta(hours=24)
        conditions = (AuditEvent.created_at >= since, AuditEvent.created_at <= until)
        total, actors = self._session.execute(
            select(
                func.count(AuditEvent.id),
                func.count(func.distinct(AuditEvent.actor)),
            ).where(*conditions)
        ).one()
        failed = self._session.scalar(
            select(func.count(AuditEvent.id)).where(
                *conditions,
                AuditEvent.action == "admin_login_failed",
            )
        )
        hour = func.date_trunc("hour", AuditEvent.created_at, "UTC")
        counts: dict[datetime, int] = dict(
            self._session.execute(
                select(hour, func.count(AuditEvent.id)).where(*conditions).group_by(hour)
            ).all()
        )
        cursor = since.replace(minute=0, second=0, microsecond=0)
        buckets = []
        while cursor <= until:
            buckets.append({"started_at": cursor, "count": counts.get(cursor, 0)})
            cursor += timedelta(hours=1)
        return {
            "total": total,
            "actors": actors,
            "failed_logins": failed or 0,
            "since": since,
            "until": until,
            "buckets": buckets,
        }
