"""Update policy rules: what a policy may say, and which one wins.

Two things live here, both pure so they can be tested without a database:

* :func:`validate_policy_definition` — what a well-formed policy is. It rejects
  the combinations that would otherwise be ambiguous at decision time (a `pin`
  without a target, a `range` with neither bound, a scope without its target).
* :func:`resolve_policies` — which policy applies to a device. Narrower scope
  wins, then higher priority; a tie at the winning priority is reported as a
  conflict rather than silently decided, because two equal-priority policies at
  the same scope express two different intentions and guessing one is how a fleet
  ends up on the wrong version (`docs/07-update-policy.md` §§13, 15).

The enums are imported from :mod:`app.db.models` because that is where every
other persisted enum in this platform is defined; there is no second definition
of `scope` or `policy_type` anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.db.models import PolicyScope, PolicyType, UpdatePolicy
from app.domain.errors import DomainError
from app.domain.version import parse_version

# Narrowest first: it is both the precedence order and the order in which the
# resolver looks for a winner.
SCOPE_PRECEDENCE = (PolicyScope.DEVICE, PolicyScope.DEVICE_TYPE, PolicyScope.GLOBAL)


def validate_policy_definition(
    *,
    scope: PolicyScope,
    policy_type: PolicyType,
    device_type_id: int | None,
    device_id: int | None,
    target_version: str | None,
    min_version: str | None,
    max_version: str | None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> None:
    """Raise :class:`DomainError` unless the policy is unambiguous."""
    if scope is PolicyScope.GLOBAL:
        if device_type_id is not None or device_id is not None:
            raise DomainError("policy_scope_target_forbidden")
    elif scope is PolicyScope.DEVICE_TYPE:
        if device_type_id is None:
            raise DomainError("policy_scope_target_required")
        if device_id is not None:
            raise DomainError("policy_scope_target_forbidden")
    elif scope is PolicyScope.DEVICE:
        if device_id is None:
            raise DomainError("policy_scope_target_required")
        if device_type_id is not None:
            raise DomainError("policy_scope_target_forbidden")

    if policy_type is PolicyType.DISABLE:
        if target_version is not None or min_version is not None or max_version is not None:
            raise DomainError("policy_version_forbidden")
    elif policy_type is PolicyType.PIN:
        if target_version is None:
            raise DomainError("policy_target_required")
        if min_version is not None or max_version is not None:
            raise DomainError("policy_version_forbidden")
        _validate_version(target_version)
    elif policy_type is PolicyType.RANGE:
        if target_version is not None:
            raise DomainError("policy_version_forbidden")
        if min_version is None and max_version is None:
            raise DomainError("policy_target_required")
        for bound in (min_version, max_version):
            if bound is not None:
                _validate_version(bound)
        if (
            min_version is not None
            and max_version is not None
            and parse_version(min_version) > parse_version(max_version)
        ):
            raise DomainError("policy_range_invalid")

    if starts_at is not None and ends_at is not None and starts_at >= ends_at:
        raise DomainError("policy_window_invalid")


def windows_overlap(
    first: tuple[datetime | None, datetime | None],
    second: tuple[datetime | None, datetime | None],
) -> bool:
    """Whether two optional time windows share any instant.

    An absent bound is open-ended, so a policy with no window overlaps
    everything — which is what makes "two equal-priority policies at one scope"
    a configuration conflict even before either starts.
    """
    first_start, first_end = first
    second_start, second_end = second
    if first_end is not None and second_start is not None and first_end <= second_start:
        return False
    return not (second_end is not None and first_start is not None and second_end <= first_start)


def is_in_effect(policy: UpdatePolicy, now: datetime) -> bool:
    """Whether a policy applies at ``now``; inactive and expired both mean no."""
    if not policy.is_active:
        return False
    if policy.starts_at is not None and policy.starts_at > now:
        return False
    return not (policy.ends_at is not None and policy.ends_at <= now)


@dataclass(frozen=True)
class PolicyResolution:
    """The single policy that governs a device, or a recorded conflict."""

    policy: UpdatePolicy | None = None
    scope: PolicyScope | None = None
    conflict: bool = False

    @property
    def policy_id(self) -> int | None:
        return self.policy.id if self.policy is not None else None


def resolve_policies(
    policies: Iterable[UpdatePolicy],
    *,
    now: datetime,
) -> PolicyResolution:
    """Pick the policy that governs a device out of its applicable set.

    The caller supplies only the policies that can reach this device (its own
    scope, its device type's scope, and global); this function does not widen
    that, it only ranks it.
    """
    in_effect = [policy for policy in policies if is_in_effect(policy, now)]
    for scope in SCOPE_PRECEDENCE:
        at_scope = [policy for policy in in_effect if policy.scope is scope]
        if not at_scope:
            continue
        highest = max(policy.priority for policy in at_scope)
        winners = [policy for policy in at_scope if policy.priority == highest]
        if len(winners) > 1:
            return PolicyResolution(policy=None, scope=scope, conflict=True)
        return PolicyResolution(policy=winners[0], scope=scope)
    return PolicyResolution()


def version_in_range(version: str, *, min_version: str | None, max_version: str | None) -> bool:
    """Inclusive range test on the numeric version triple."""
    parsed = parse_version(version)
    if min_version is not None and parsed < parse_version(min_version):
        return False
    return not (max_version is not None and parsed > parse_version(max_version))


def _validate_version(version: str) -> None:
    try:
        parse_version(version)
    except ValueError:
        raise DomainError("invalid_version") from None
