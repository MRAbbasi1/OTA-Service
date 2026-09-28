"""Eligibility decision matrix.

The decision service is the only place eligibility exists, so these tests are the
contract for it: every documented reason, the policy precedence rules, and the
things a policy is *not* allowed to do (`docs/07-update-policy.md`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Device,
    DeviceStatus,
    DeviceType,
    FirmwareRelease,
    FirmwareReleaseStatus,
    PolicyScope,
    PolicyType,
    UpdatePolicy,
)
from app.services.firmware import FirmwareService
from app.services.policies import PolicyService
from app.services.update_decision import UpdateDecisionService, UpdateReason
from tests.conftest import FakeObjectStorage

CODE = "bcs-controller-v1"
PLATFORM = "esp32-s3"

NOW = datetime.now(UTC)


def firmware_service(session: Session) -> FirmwareService:
    settings = get_settings()
    return FirmwareService(
        session,
        FakeObjectStorage(),
        manifest_base_url=str(settings.ota_manifest_base_url),
        firmware_base_url=str(settings.ota_firmware_base_url),
        bucket=settings.minio_bucket,
    )


def device_type(session: Session, code: str = CODE, platform: str = PLATFORM) -> DeviceType:
    row = DeviceType(code=code, name="Smart Controller", platform=platform)
    session.add(row)
    session.flush()
    return row


def device(
    session: Session,
    device_type_row: DeviceType,
    *,
    status: DeviceStatus = DeviceStatus.ACTIVE,
    ota_enabled: bool = True,
    serial: int = 10432,
    known_version: str | None = None,
) -> Device:
    row = Device(
        device_type_id=device_type_row.id,
        serial_number=serial,
        raw_efuse_mac=f"7C:9E:BD:00:{serial // 256 % 256:02X}:{serial % 256:02X}",
        status=status,
        ota_enabled=ota_enabled,
    )
    if known_version is not None:
        row.current_firmware_version = known_version
        row.known_firmware_source = "admin_asserted"
    session.add(row)
    session.flush()
    return row


def release(
    session: Session,
    device_type_row: DeviceType,
    version: str,
    status: FirmwareReleaseStatus = FirmwareReleaseStatus.PUBLISHED,
) -> FirmwareRelease:
    row = FirmwareRelease(
        device_type_id=device_type_row.id,
        version=version,
        status=status,
        created_by="tester",
    )
    session.add(row)
    session.flush()
    return row


def policy(session: Session, **fields: Any) -> UpdatePolicy:
    """Create a policy through the service, so its rules are the ones under test."""
    return PolicyService(session).create(name="policy", **fields)


def decide(session: Session, row: Device, *, code: str = CODE, platform: str = PLATFORM):
    return UpdateDecisionService(
        session, firmware_service(session), PolicyService(session)
    ).decide_for_device(row, requested_code=code, requested_platform=platform)


# --- default behaviour -----------------------------------------------------------


def test_latest_published_release_is_offered(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")
    release(db_session, type_row, "2.7.0", FirmwareReleaseStatus.DRAFT)

    decision = decide(db_session, device(db_session, type_row))

    assert decision.update_available is True
    assert decision.reason is UpdateReason.LATEST_AVAILABLE
    assert decision.release is not None and decision.release.version == "2.6.1"
    assert decision.policy_id is None and decision.policy_scope is None


def test_selection_is_numeric_not_lexicographic(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.9.0")
    release(db_session, type_row, "2.10.0")
    release(db_session, type_row, "2.10.1")

    decision = decide(db_session, device(db_session, type_row))

    assert decision.target_version == "2.10.1"


def test_a_release_of_another_device_type_is_never_selected(db_session: Session) -> None:
    type_row = device_type(db_session)
    other = device_type(db_session, code="bcs-sensor-v1", platform="esp32-s3")
    release(db_session, other, "9.9.9")

    decision = decide(db_session, device(db_session, type_row))

    assert decision.update_available is False
    assert decision.reason is UpdateReason.NO_ELIGIBLE_RELEASE


def test_path_identity_mismatch_is_not_an_offer(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")
    row = device(db_session, type_row, known_version="2.5.0")

    assert decide(db_session, row, code="bcs-sensor-v1").reason is UpdateReason.DEVICE_TYPE_MISMATCH
    assert decide(db_session, row, platform="esp32-c3").reason is UpdateReason.DEVICE_TYPE_MISMATCH


def test_known_version_blocks_an_equal_or_older_release(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")

    equal = decide(db_session, device(db_session, type_row, known_version="2.6.1"))
    higher = decide(db_session, device(db_session, type_row, known_version="2.6.2", serial=2))

    assert equal.update_available is False and equal.reason is UpdateReason.NO_UPDATE
    assert higher.update_available is False and higher.reason is UpdateReason.NO_UPDATE


def test_a_strictly_newer_release_is_offered(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")

    decision = decide(db_session, device(db_session, type_row, known_version="2.6.0"))

    assert decision.update_available is True
    assert decision.known_version == "2.6.0"
    assert decision.known_version_source == "admin_asserted"


def test_an_unparseable_known_version_degrades_to_unknown(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")

    decision = decide(db_session, device(db_session, type_row, known_version="v2.6.1"))

    assert decision.update_available is True
    assert decision.known_version is None


def test_disabled_ota_flag_is_not_an_offer(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")

    decision = decide(db_session, device(db_session, type_row, ota_enabled=False))

    assert decision.reason is UpdateReason.OTA_DISABLED


def test_an_inactive_device_is_not_an_offer(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.1")

    decision = decide(db_session, device(db_session, type_row, status=DeviceStatus.PROVISIONED))

    assert decision.reason is UpdateReason.DEVICE_INACTIVE


# --- policy: scope and precedence ------------------------------------------------


def test_a_global_pin_narrows_every_device(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.7")
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    only_258 = policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.update_available is True
    assert decision.reason is UpdateReason.PINNED_VERSION
    assert decision.target_version == "2.5.8"
    assert decision.policy_id == only_258.id
    assert decision.policy_scope is PolicyScope.GLOBAL


def test_a_device_policy_wins_over_a_device_type_policy(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    row = device(db_session, type_row, known_version="2.5.7")
    policy(
        db_session,
        scope=PolicyScope.DEVICE_TYPE,
        device_type_id=type_row.id,
        policy_type=PolicyType.PIN,
        target_version="2.6.0",
    )
    per_device = policy(
        db_session,
        scope=PolicyScope.DEVICE,
        device_id=row.id,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    decision = decide(db_session, row)

    assert decision.target_version == "2.5.8"
    assert decision.policy_id == per_device.id


def test_a_device_type_policy_wins_over_a_global_one(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.6.0",
    )
    scoped = policy(
        db_session,
        scope=PolicyScope.DEVICE_TYPE,
        device_type_id=type_row.id,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.target_version == "2.5.8"
    assert decision.policy_id == scoped.id


def test_a_higher_priority_policy_wins_within_one_scope(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.6.0",
        priority=1,
    )
    higher = policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
        priority=2,
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.target_version == "2.5.8"
    assert decision.policy_id == higher.id


def test_two_equal_priority_policies_at_one_scope_fail_closed(db_session: Session) -> None:
    """Inserted directly, because the service refuses to create this ambiguity."""
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.0")
    for target in ("2.5.8", "2.6.0"):
        db_session.add(
            UpdatePolicy(
                name=f"pin {target}",
                scope=PolicyScope.GLOBAL,
                policy_type=PolicyType.PIN,
                target_version=target,
                priority=0,
                created_by="tester",
            )
        )
    db_session.flush()

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.update_available is False
    assert decision.reason is UpdateReason.POLICY_BLOCKED
    assert decision.policy_id is None


def test_an_inactive_or_expired_policy_changes_nothing(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    service = PolicyService(db_session)
    inactive = policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
        priority=1,
    )
    service.set_active(inactive.id, False)
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
        priority=2,
        starts_at=NOW - timedelta(days=2),
        ends_at=NOW - timedelta(days=1),
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.target_version == "2.6.0"
    assert decision.policy_id is None
    assert inactive.is_active is False


def test_a_future_policy_does_not_apply_yet(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
        starts_at=NOW + timedelta(days=1),
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.target_version == "2.6.0"


# --- policy: what each type does -------------------------------------------------


def test_a_range_policy_offers_the_newest_release_inside_it(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.5")
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.5.9")
    release(db_session, type_row, "2.6.0")
    in_range = policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.RANGE,
        min_version="2.5.5",
        max_version="2.5.9",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.5"))

    assert decision.update_available is True
    assert decision.reason is UpdateReason.VERSION_RANGE
    assert decision.target_version == "2.5.9"
    assert decision.policy_id == in_range.id


def test_a_range_that_excludes_every_release_is_policy_blocked(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.0")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.RANGE,
        min_version="2.5.0",
        max_version="2.5.9",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.0"))

    assert decision.reason is UpdateReason.POLICY_BLOCKED


def test_a_pin_to_an_unpublished_version_is_policy_blocked(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.0")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.reason is UpdateReason.POLICY_BLOCKED


def test_a_disable_policy_withholds_the_offer(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.6.0")
    stopper = policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.DISABLE,
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.update_available is False
    assert decision.reason is UpdateReason.OTA_DISABLED
    assert decision.policy_id == stopper.id


def test_a_pin_below_the_known_version_withholds_rather_than_downgrades(
    db_session: Session,
) -> None:
    """The firmware installs only a strictly newer version, so this is no-offer."""
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.7.0")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.7.0"))

    assert decision.update_available is False
    assert decision.reason is UpdateReason.NO_UPDATE
    assert decision.target_version == "2.5.8"


def test_no_published_release_is_a_catalog_state_not_a_policy_block(
    db_session: Session,
) -> None:
    type_row = device_type(db_session)
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.RANGE,
        min_version="2.5.0",
    )

    decision = decide(db_session, device(db_session, type_row))

    assert decision.reason is UpdateReason.NO_ELIGIBLE_RELEASE


# --- policy: what it must never do ------------------------------------------------


def test_a_policy_cannot_reach_another_device_types_release(db_session: Session) -> None:
    type_row = device_type(db_session)
    other = device_type(db_session, code="bcs-sensor-v1", platform="esp32-s3")
    release(db_session, other, "9.9.9")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="9.9.9",
    )

    decision = decide(db_session, device(db_session, type_row))

    assert decision.update_available is False
    assert decision.reason is UpdateReason.NO_ELIGIBLE_RELEASE


def test_a_device_policy_does_not_touch_another_device(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8")
    release(db_session, type_row, "2.6.0")
    pinned = device(db_session, type_row, serial=10432, known_version="2.5.7")
    other = device(db_session, type_row, serial=10433, known_version="2.5.7")
    policy(
        db_session,
        scope=PolicyScope.DEVICE,
        device_id=pinned.id,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    assert decide(db_session, pinned).target_version == "2.5.8"
    assert decide(db_session, other).target_version == "2.6.0"


def test_a_deprecated_release_is_never_offered_even_when_pinned(db_session: Session) -> None:
    type_row = device_type(db_session)
    release(db_session, type_row, "2.5.8", FirmwareReleaseStatus.DEPRECATED)
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    decision = decide(db_session, device(db_session, type_row, known_version="2.5.7"))

    assert decision.reason is UpdateReason.NO_ELIGIBLE_RELEASE


# --- delivery gate ----------------------------------------------------------------


def delivery(session: Session, row: Device, release_row: FirmwareRelease) -> bool:
    return UpdateDecisionService(
        session, firmware_service(session), PolicyService(session)
    ).allows_delivery(row, release_row)


def test_delivery_follows_the_same_policy_as_the_offer(db_session: Session) -> None:
    type_row = device_type(db_session)
    pinned = release(db_session, type_row, "2.5.8")
    newer = release(db_session, type_row, "2.6.0")
    row = device(db_session, type_row, known_version="2.5.7")
    policy(
        db_session,
        scope=PolicyScope.GLOBAL,
        policy_type=PolicyType.PIN,
        target_version="2.5.8",
    )

    assert delivery(db_session, row, pinned) is True
    assert delivery(db_session, row, newer) is False


def test_a_disable_policy_blocks_the_bytes_not_only_the_offer(db_session: Session) -> None:
    type_row = device_type(db_session)
    published = release(db_session, type_row, "2.6.0")
    row = device(db_session, type_row, known_version="2.5.7")
    policy(db_session, scope=PolicyScope.GLOBAL, policy_type=PolicyType.DISABLE)

    assert delivery(db_session, row, published) is False


def test_without_a_policy_every_deliverable_release_is_allowed(db_session: Session) -> None:
    type_row = device_type(db_session)
    published = release(db_session, type_row, "2.6.0")

    assert delivery(db_session, device(db_session, type_row), published) is True
