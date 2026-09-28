"""Unit tests for the policy rules themselves: no database, no HTTP.

These are the rules that decide which version a fleet lands on, so they are
tested directly rather than only through an endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db.models import PolicyScope, PolicyType, UpdatePolicy
from app.domain.errors import DomainError
from app.domain.policy import (
    resolve_policies,
    validate_policy_definition,
    version_in_range,
    windows_overlap,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def make(**fields: Any) -> UpdatePolicy:
    """A transient policy row: these tests never touch a session."""
    base: dict[str, Any] = {
        "name": "p",
        "scope": PolicyScope.GLOBAL,
        "policy_type": PolicyType.DISABLE,
        "priority": 0,
        "is_active": True,
    }
    base.update(fields)
    return UpdatePolicy(**base)


def definition(**fields: Any) -> None:
    base: dict[str, Any] = {
        "scope": PolicyScope.GLOBAL,
        "policy_type": PolicyType.PIN,
        "device_type_id": None,
        "device_id": None,
        "target_version": "2.5.8",
        "min_version": None,
        "max_version": None,
        "starts_at": None,
        "ends_at": None,
    }
    base.update(fields)
    validate_policy_definition(**base)


class TestScopeTarget:
    def test_a_global_policy_may_not_name_a_target(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(device_id=7)
        assert exc.value.code == "policy_scope_target_forbidden"

    def test_a_device_type_policy_requires_its_type(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(scope=PolicyScope.DEVICE_TYPE)
        assert exc.value.code == "policy_scope_target_required"

    def test_a_device_policy_requires_its_device(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(scope=PolicyScope.DEVICE)
        assert exc.value.code == "policy_scope_target_required"

    def test_a_device_policy_may_not_also_name_a_type(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(scope=PolicyScope.DEVICE, device_id=7, device_type_id=3)
        assert exc.value.code == "policy_scope_target_forbidden"

    def test_valid_scopes_pass(self) -> None:
        definition()
        definition(scope=PolicyScope.DEVICE_TYPE, device_type_id=3)
        definition(scope=PolicyScope.DEVICE, device_id=7)


class TestPolicyTypeFields:
    def test_a_pin_requires_a_target(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(target_version=None)
        assert exc.value.code == "policy_target_required"

    def test_a_pin_rejects_range_bounds(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(min_version="2.5.0")
        assert exc.value.code == "policy_version_forbidden"

    def test_a_range_needs_at_least_one_bound(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(policy_type=PolicyType.RANGE, target_version=None)
        assert exc.value.code == "policy_target_required"

    def test_a_range_must_be_ordered(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(
                policy_type=PolicyType.RANGE,
                target_version=None,
                min_version="2.6.0",
                max_version="2.5.0",
            )
        assert exc.value.code == "policy_range_invalid"

    def test_a_disable_carries_no_version(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(policy_type=PolicyType.DISABLE)
        assert exc.value.code == "policy_version_forbidden"

    def test_a_one_sided_range_and_a_valid_disable_pass(self) -> None:
        definition(
            policy_type=PolicyType.RANGE, target_version=None, min_version="2.5.0", max_version=None
        )
        definition(policy_type=PolicyType.DISABLE, target_version=None)

    def test_versions_must_be_the_numeric_triple(self) -> None:
        for bad in ("v2.5.8", "2.5", "2.5.8-rc1", "latest", "02.5.8", "2.5.8.1"):
            with pytest.raises(DomainError) as exc:
                definition(target_version=bad)
            assert exc.value.code == "invalid_version", bad

    def test_a_backwards_window_is_rejected(self) -> None:
        with pytest.raises(DomainError) as exc:
            definition(starts_at=NOW, ends_at=NOW - timedelta(hours=1))
        assert exc.value.code == "policy_window_invalid"


class TestResolution:
    def test_no_policy_resolves_to_nothing(self) -> None:
        resolution = resolve_policies([], now=NOW)
        assert resolution.policy is None and resolution.conflict is False

    def test_the_narrowest_scope_wins(self) -> None:
        global_row = make(priority=99)
        type_row = make(scope=PolicyScope.DEVICE_TYPE, device_type_id=1, priority=1)
        device_row = make(scope=PolicyScope.DEVICE, device_id=5, priority=0)

        resolution = resolve_policies([global_row, type_row, device_row], now=NOW)

        assert resolution.policy is device_row
        assert resolution.scope is PolicyScope.DEVICE

    def test_higher_priority_wins_within_a_scope(self) -> None:
        low = make(priority=1)
        high = make(priority=2)

        assert resolve_policies([low, high], now=NOW).policy is high

    def test_a_tie_at_the_winning_priority_is_a_conflict(self) -> None:
        first = make(priority=5)
        second = make(priority=5)

        resolution = resolve_policies([first, second], now=NOW)

        assert resolution.policy is None
        assert resolution.conflict is True
        assert resolution.scope is PolicyScope.GLOBAL

    def test_a_narrower_conflict_does_not_fall_back_to_a_wider_policy(self) -> None:
        """Ambiguity at the scope that governs the device is not a reason to
        apply a policy that governs it less directly."""
        global_row = make(scope=PolicyScope.GLOBAL, priority=0)
        tied_a = make(scope=PolicyScope.DEVICE, device_id=5, priority=0)
        tied_b = make(scope=PolicyScope.DEVICE, device_id=5, priority=0)

        resolution = resolve_policies([global_row, tied_a, tied_b], now=NOW)

        assert resolution.conflict is True
        assert resolution.scope is PolicyScope.DEVICE

    def test_a_lower_priority_tie_does_not_matter(self) -> None:
        winner = make(priority=10)
        tied_a = make(priority=1)
        tied_b = make(priority=1)

        assert resolve_policies([winner, tied_a, tied_b], now=NOW).policy is winner

    def test_inactive_started_and_ended_policies_are_ignored(self) -> None:
        inactive = make(is_active=False)
        future = make(starts_at=NOW + timedelta(minutes=1))
        ended = make(ends_at=NOW)
        live = make(ends_at=NOW + timedelta(minutes=1))

        assert resolve_policies([inactive, future, ended, live], now=NOW).policy is live

    def test_a_policy_with_no_window_is_always_in_effect(self) -> None:
        assert resolve_policies([make()], now=NOW).policy is not None


class TestWindows:
    def test_an_open_window_overlaps_everything(self) -> None:
        assert windows_overlap((None, None), (NOW, NOW + timedelta(hours=1))) is True

    def test_disjoint_windows_do_not_overlap(self) -> None:
        assert (
            windows_overlap((NOW, NOW + timedelta(hours=1)), (NOW + timedelta(hours=1), None))
            is False
        )

    def test_touching_windows_are_disjoint(self) -> None:
        first = (NOW, NOW + timedelta(hours=1))
        second = (NOW - timedelta(hours=1), NOW)
        assert windows_overlap(first, second) is False


class TestRange:
    def test_bounds_are_inclusive(self) -> None:
        assert version_in_range("2.5.5", min_version="2.5.5", max_version="2.5.9") is True
        assert version_in_range("2.5.9", min_version="2.5.5", max_version="2.5.9") is True

    def test_outside_the_bounds_is_excluded(self) -> None:
        assert version_in_range("2.5.4", min_version="2.5.5", max_version="2.5.9") is False
        assert version_in_range("2.6.0", min_version="2.5.5", max_version="2.5.9") is False

    def test_one_sided_ranges(self) -> None:
        assert version_in_range("9.9.9", min_version="2.5.5", max_version=None) is True
        assert version_in_range("2.0.0", min_version=None, max_version="2.5.9") is True

    def test_comparison_is_numeric(self) -> None:
        assert version_in_range("2.10.0", min_version="2.9.0", max_version=None) is True
