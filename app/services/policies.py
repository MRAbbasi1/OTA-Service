"""Update policy administration and resolution against the database.

The rules live in :mod:`app.domain.policy`; this service is the only place that
reads and writes policy rows, so "which policy applies" is answered once and the
device endpoint, the dashboard preview, and any future rollout job all get the
same answer (`docs/07-update-policy.md` §18).

Two guarantees are enforced here rather than in the API:

* a policy can never be created or edited into an ambiguous configuration;
* a policy can never change a device's device type or OTA path — it has no field
  that could express either.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Device, DeviceType, PolicyScope, PolicyType, UpdatePolicy
from app.domain.errors import DomainError
from app.domain.policy import (
    PolicyResolution,
    resolve_policies,
    validate_policy_definition,
    windows_overlap,
)


class PolicyService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- writes -------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        scope: PolicyScope,
        policy_type: PolicyType,
        device_type_id: int | None = None,
        device_id: int | None = None,
        target_version: str | None = None,
        min_version: str | None = None,
        max_version: str | None = None,
        priority: int = 0,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        description: str | None = None,
        created_by: str = "admin",
    ) -> UpdatePolicy:
        self._assert_definition(
            scope=scope,
            policy_type=policy_type,
            device_type_id=device_type_id,
            device_id=device_id,
            target_version=target_version,
            min_version=min_version,
            max_version=max_version,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        self._assert_no_conflict(
            scope=scope,
            device_type_id=device_type_id,
            device_id=device_id,
            priority=priority,
            starts_at=starts_at,
            ends_at=ends_at,
            exclude_id=None,
        )
        policy = UpdatePolicy(
            name=name,
            description=description,
            scope=scope,
            device_type_id=device_type_id,
            device_id=device_id,
            policy_type=policy_type,
            target_version=target_version,
            min_version=min_version,
            max_version=max_version,
            priority=priority,
            starts_at=starts_at,
            ends_at=ends_at,
            created_by=created_by,
        )
        self._session.add(policy)
        self._session.flush()
        return policy

    def update(self, policy_id: int, changes: dict[str, object]) -> UpdatePolicy:
        """Apply a partial edit, re-validating the whole policy afterwards.

        Scope and policy type are immutable: changing either is a different
        policy, and rewriting it in place would silently re-interpret every
        decision it has already explained.
        """
        policy = self.get(policy_id)
        immutable = {"scope", "policy_type", "device_type_id", "device_id"}
        if immutable & set(changes):
            raise DomainError("policy_immutable")
        for field, value in changes.items():
            setattr(policy, field, value)

        self._assert_definition(
            scope=policy.scope,
            policy_type=policy.policy_type,
            device_type_id=policy.device_type_id,
            device_id=policy.device_id,
            target_version=policy.target_version,
            min_version=policy.min_version,
            max_version=policy.max_version,
            starts_at=policy.starts_at,
            ends_at=policy.ends_at,
        )
        self._assert_no_conflict(
            scope=policy.scope,
            device_type_id=policy.device_type_id,
            device_id=policy.device_id,
            priority=policy.priority,
            starts_at=policy.starts_at,
            ends_at=policy.ends_at,
            exclude_id=policy.id,
        )
        self._session.flush()
        return policy

    def set_active(self, policy_id: int, is_active: bool) -> UpdatePolicy:
        policy = self.get(policy_id)
        if is_active:
            self._assert_no_conflict(
                scope=policy.scope,
                device_type_id=policy.device_type_id,
                device_id=policy.device_id,
                priority=policy.priority,
                starts_at=policy.starts_at,
                ends_at=policy.ends_at,
                exclude_id=policy.id,
            )
        policy.is_active = is_active
        self._session.flush()
        return policy

    def delete(self, policy_id: int) -> None:
        self._session.delete(self.get(policy_id))
        self._session.flush()

    # --- reads --------------------------------------------------------------------

    def get(self, policy_id: int) -> UpdatePolicy:
        policy = self._session.get(UpdatePolicy, policy_id)
        if policy is None:
            raise DomainError("policy_not_found")
        return policy

    def list_policies(
        self,
        *,
        scope: PolicyScope | None = None,
        is_active: bool | None = None,
        device_type_id: int | None = None,
        device_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[UpdatePolicy], int]:
        conditions = []
        if scope is not None:
            conditions.append(UpdatePolicy.scope == scope)
        if is_active is not None:
            conditions.append(UpdatePolicy.is_active.is_(is_active))
        if device_type_id is not None:
            conditions.append(UpdatePolicy.device_type_id == device_type_id)
        if device_id is not None:
            conditions.append(UpdatePolicy.device_id == device_id)
        stmt = select(UpdatePolicy)
        if conditions:
            stmt = stmt.where(*conditions)
        total = self._session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        rows = self._session.execute(
            stmt.order_by(UpdatePolicy.id.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
        return list(rows), int(total)

    def affected_devices(
        self, policy_id: int, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[Device], int]:
        """The devices a policy can reach, before time windows are considered.

        A global policy reaches every device, which is exactly why the count is
        returned with the list: an operator should see the blast radius before
        enabling it.
        """
        policy = self.get(policy_id)
        stmt = select(Device).options(selectinload(Device.device_type))
        if policy.scope is PolicyScope.DEVICE:
            stmt = stmt.where(Device.id == policy.device_id)
        elif policy.scope is PolicyScope.DEVICE_TYPE:
            stmt = stmt.where(Device.device_type_id == policy.device_type_id)
        total = self._session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        rows = self._session.execute(
            stmt.order_by(Device.id).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
        return list(rows), int(total)

    def resolve_for_device(
        self, device: Device, *, now: datetime | None = None
    ) -> PolicyResolution:
        """Resolve the governing policy for one device.

        Only the three scopes that can reach this device are fetched; the
        ranking itself is the pure function in :mod:`app.domain.policy`.
        """
        if device.device_type_id is None:  # defensive: cannot happen, the column is NOT NULL
            return PolicyResolution()
        rows = self._session.execute(
            select(UpdatePolicy).where(
                UpdatePolicy.is_active.is_(True),
                or_(
                    UpdatePolicy.scope == PolicyScope.GLOBAL,
                    (
                        (UpdatePolicy.scope == PolicyScope.DEVICE_TYPE)
                        & (UpdatePolicy.device_type_id == device.device_type_id)
                    ),
                    (
                        (UpdatePolicy.scope == PolicyScope.DEVICE)
                        & (UpdatePolicy.device_id == device.id)
                    ),
                ),
            )
        ).scalars()
        return resolve_policies(rows, now=now or datetime.now(UTC))

    # --- internals ----------------------------------------------------------------

    def _assert_definition(
        self,
        *,
        scope: PolicyScope,
        policy_type: PolicyType,
        device_type_id: int | None,
        device_id: int | None,
        target_version: str | None,
        min_version: str | None,
        max_version: str | None,
        starts_at: datetime | None,
        ends_at: datetime | None,
    ) -> None:
        validate_policy_definition(
            scope=scope,
            policy_type=policy_type,
            device_type_id=device_type_id,
            device_id=device_id,
            target_version=target_version,
            min_version=min_version,
            max_version=max_version,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        # A policy pointing at nothing is a silent no-op, so the target must exist.
        if scope is PolicyScope.DEVICE_TYPE:
            device_type = self._session.get(DeviceType, device_type_id)
            if device_type is None:
                raise DomainError("device_type_not_found")
        elif scope is PolicyScope.DEVICE:
            if self._session.get(Device, device_id) is None:
                raise DomainError("device_not_found")

    def _assert_no_conflict(
        self,
        *,
        scope: PolicyScope,
        device_type_id: int | None,
        device_id: int | None,
        priority: int,
        starts_at: datetime | None,
        ends_at: datetime | None,
        exclude_id: int | None,
    ) -> None:
        """Reject a second equal-priority policy with an overlapping window.

        Two policies at one scope with the same priority are two different
        intentions; the decision cannot be derived from the data, so it is
        rejected instead of guessed (`docs/07-update-policy.md` §15).
        """
        conditions = [UpdatePolicy.scope == scope, UpdatePolicy.is_active.is_(True)]
        if scope is PolicyScope.DEVICE_TYPE:
            conditions.append(UpdatePolicy.device_type_id == device_type_id)
        elif scope is PolicyScope.DEVICE:
            conditions.append(UpdatePolicy.device_id == device_id)
        stmt = select(UpdatePolicy).where(*conditions, UpdatePolicy.priority == priority)
        if exclude_id is not None:
            stmt = stmt.where(UpdatePolicy.id != exclude_id)
        for other in self._session.execute(stmt).scalars():
            if windows_overlap((starts_at, ends_at), (other.starts_at, other.ends_at)):
                raise DomainError("policy_conflict")
