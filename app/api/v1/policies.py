"""Admin update policy endpoints.

Policy is the only lever that narrows eligibility, so every write here is
audited and every rejection names the reason: an operator creating a policy that
would silently override another one should be told at creation time, not discover
it from a fleet stuck on the wrong version.

Nothing in this module decides eligibility. It stores and edits rules; the single
decision service interprets them (``docs/07-update-policy.md`` §18).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_session, get_settings
from app.api.v1.devices import device_read
from app.api.v1.errors import raise_domain_error
from app.api.v1.schemas import (
    PolicyActiveUpdate,
    UpdatePolicyCreate,
    UpdatePolicyRead,
    UpdatePolicyUpdate,
)
from app.api.v1.security import PoliciesRead, PoliciesWrite
from app.core.config import Settings
from app.db.models import PolicyScope, UpdatePolicy
from app.domain.errors import DomainError
from app.services.audit import AuditService
from app.services.policies import PolicyService

router = APIRouter(prefix="/api/v1/admin/policies", tags=["admin"])


def _read(policy: UpdatePolicy) -> UpdatePolicyRead:
    return UpdatePolicyRead.model_validate(policy, from_attributes=True)


def _get_or_404(session: Session, policy_id: int) -> UpdatePolicy:
    try:
        return PolicyService(session).get(policy_id)
    except DomainError as exc:
        raise_domain_error(exc)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_policy(
    payload: UpdatePolicyCreate,
    admin: PoliciesWrite,
    session: Session = Depends(get_session),
) -> UpdatePolicyRead:
    service = PolicyService(session)
    try:
        policy = service.create(**payload.model_dump(), created_by=admin.email)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "policy_created",
        "update_policy",
        policy.id,
        {
            "scope": policy.scope.value,
            "policy_type": policy.policy_type.value,
            "priority": policy.priority,
            "device_id": policy.device_id,
            "device_type_id": policy.device_type_id,
        },
    )
    session.commit()
    return _read(policy)


@router.get("")
def list_policies(
    admin: PoliciesRead,
    scope: PolicyScope | None = None,
    is_active: bool | None = None,
    device_type_id: int | None = None,
    device_id: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    policies, total = PolicyService(session).list_policies(
        scope=scope,
        is_active=is_active,
        device_type_id=device_type_id,
        device_id=device_id,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [_read(policy) for policy in policies],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{policy_id}")
def get_policy(
    policy_id: int, admin: PoliciesRead, session: Session = Depends(get_session)
) -> UpdatePolicyRead:
    return _read(_get_or_404(session, policy_id))


@router.patch("/{policy_id}")
def update_policy(
    policy_id: int,
    payload: UpdatePolicyUpdate,
    admin: PoliciesWrite,
    session: Session = Depends(get_session),
) -> UpdatePolicyRead:
    changes = payload.model_dump(exclude_unset=True)
    try:
        policy = PolicyService(session).update(policy_id, changes)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "policy_updated",
        "update_policy",
        policy_id,
        {"changed": sorted(changes)},
    )
    session.commit()
    return _read(policy)


@router.post("/{policy_id}/active")
def set_policy_active(
    policy_id: int,
    payload: PolicyActiveUpdate,
    admin: PoliciesWrite,
    session: Session = Depends(get_session),
) -> UpdatePolicyRead:
    """Enable or disable a policy without deleting its history."""
    try:
        policy = PolicyService(session).set_active(policy_id, payload.is_active)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "policy_activated" if payload.is_active else "policy_deactivated",
        "update_policy",
        policy_id,
    )
    session.commit()
    return _read(policy)


@router.delete("/{policy_id}")
def delete_policy(
    policy_id: int, admin: PoliciesWrite, session: Session = Depends(get_session)
) -> dict[str, str]:
    try:
        PolicyService(session).delete(policy_id)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(admin.email, "policy_deleted", "update_policy", policy_id)
    session.commit()
    return {"status": "deleted"}


@router.get("/{policy_id}/devices")
def list_affected_devices(
    policy_id: int,
    admin: PoliciesRead,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """The devices this policy can reach — its blast radius, before enabling it."""
    devices, total = PolicyService(session).affected_devices(
        policy_id, page=page, page_size=page_size
    )
    return {
        "items": [device_read(device, settings) for device in devices],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
