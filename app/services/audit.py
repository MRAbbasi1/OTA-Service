from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent

logger = logging.getLogger(__name__)


class AuditService:
    """Append-only audit trail; never stores secrets.

    "Never stores secrets" is enforced by the callers that have secrets: none of
    them passes a password, a device token, or a key into `record`, and several
    only ever record the fact that such a value was replaced.
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
        event = AuditEvent(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            detail=detail,
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
    ) -> tuple[list[AuditEvent], int]:
        """Read the trail, newest first. There is no update or delete."""
        conditions = []
        if action:
            conditions.append(AuditEvent.action == action)
        if actor:
            conditions.append(AuditEvent.actor == actor)
        if resource_type:
            conditions.append(AuditEvent.resource_type == resource_type)
        if resource_id:
            conditions.append(AuditEvent.resource_id == resource_id)
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
