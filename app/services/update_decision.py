"""Deterministic firmware eligibility.

Every decision about whether a device may be offered a release lives here, so the
device-facing endpoint, the dashboard preview, and any future rollout job agree
by construction (``docs/07-update-policy.md`` §18). No route handler may decide
eligibility.

The rule, in the documented order (``docs/07-update-policy.md`` §9):

```text
path identity matches the device's own device type
device is ACTIVE
the device's own OTA flag is set
the governing policy  (device > device type > global, then priority)
the published releases of the device's own device type, narrowed by that policy
nothing older than or equal to the known firmware version
```

Two facts constrain the rule and must not be papered over:

* the OTA request carries no running firmware version, so ``known_version`` can
  only be administrator-asserted today;
* the device itself accepts only a strictly newer version, so a pin below the
  known version withholds an offer rather than causing a downgrade, and a policy
  can never express a downgrade at all.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import (
    Device,
    DeviceStatus,
    DeviceType,
    FirmwareRelease,
    PolicyScope,
    PolicyType,
    UpdatePolicy,
)
from app.domain.policy import version_in_range
from app.domain.version import parse_version
from app.services.firmware import FirmwareService
from app.services.policies import PolicyService

logger = logging.getLogger(__name__)


class UpdateReason(enum.StrEnum):
    """Why the decision came out the way it did; part of the API contract."""

    LATEST_AVAILABLE = "LATEST_AVAILABLE"
    PINNED_VERSION = "PINNED_VERSION"
    VERSION_RANGE = "VERSION_RANGE"
    DEVICE_TYPE_MISMATCH = "DEVICE_TYPE_MISMATCH"
    DEVICE_INACTIVE = "DEVICE_INACTIVE"
    OTA_DISABLED = "OTA_DISABLED"
    NO_ELIGIBLE_RELEASE = "NO_ELIGIBLE_RELEASE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    NO_UPDATE = "NO_UPDATE"


@dataclass(frozen=True)
class UpdateDecision:
    """The single answer to "may this device be offered firmware?".

    It carries the policy that produced it, so an administrator can be told why
    without re-deriving the answer, and `known_version` is always shown with its
    provenance because it is an assertion rather than an observation.
    """

    update_available: bool
    reason: UpdateReason
    device_id: int | None = None
    known_version: str | None = None
    known_version_source: str | None = None
    release: FirmwareRelease | None = None
    policy_id: int | None = None
    policy_scope: PolicyScope | None = None

    @property
    def release_id(self) -> int | None:
        return self.release.id if self.release is not None else None

    @property
    def target_version(self) -> str | None:
        return self.release.version if self.release is not None else None


class UpdateDecisionService:
    def __init__(
        self, session: Session, firmware: FirmwareService, policies: PolicyService
    ) -> None:
        self._session = session
        self._firmware = firmware
        self._policies = policies

    def check_eligibility(
        self,
        device: Device,
        *,
        requested_code: str,
        requested_platform: str,
    ) -> UpdateReason | None:
        """Return the reason a device may not receive firmware, or ``None``.

        This is the entitlement boundary shared by both OTA endpoints: the
        manifest endpoint then selects a release, and the firmware endpoint serves
        one specific already-signed release. It deliberately does not consult
        releases, because entitlement that depends on a rotating catalog would
        revoke a download the device is already in the middle of.
        """
        device_type = self._session.get(DeviceType, device.device_type_id)
        if device_type is None:
            logger.error(
                "device_has_no_device_type",
                extra={"extra_fields": {"device_id": device.id}},
            )
            return UpdateReason.NO_ELIGIBLE_RELEASE
        if (requested_code, requested_platform) != (device_type.code, device_type.platform):
            # Entitlement is always decided against the authenticated device's own
            # device type, so a different path cannot grant access to another
            # family's firmware. Not 403: see docs/16 §10.3.
            return UpdateReason.DEVICE_TYPE_MISMATCH
        if device.status is not DeviceStatus.ACTIVE:
            return UpdateReason.DEVICE_INACTIVE
        if not device.ota_enabled:
            return UpdateReason.OTA_DISABLED
        return None

    def decide_for_device(
        self,
        device: Device,
        *,
        requested_code: str,
        requested_platform: str,
    ) -> UpdateDecision:
        reason = self.check_eligibility(
            device, requested_code=requested_code, requested_platform=requested_platform
        )
        known_version = self._known_version(device)
        if reason is not None:
            return UpdateDecision(
                update_available=False,
                reason=reason,
                device_id=device.id,
                known_version=known_version,
                known_version_source=device.known_firmware_source,
            )

        resolution = self._policies.resolve_for_device(device)
        if resolution.conflict:
            # Fail closed: two equal-priority policies express two different
            # intentions, and offering a version under either one could be wrong.
            logger.error(
                "policy_conflict_at_decision_time",
                extra={"extra_fields": {"device_id": device.id, "scope": str(resolution.scope)}},
            )
            return UpdateDecision(
                update_available=False,
                reason=UpdateReason.POLICY_BLOCKED,
                device_id=device.id,
                known_version=known_version,
                known_version_source=device.known_firmware_source,
                policy_scope=resolution.scope,
            )

        policy = resolution.policy
        if policy is not None and policy.policy_type is PolicyType.DISABLE:
            return UpdateDecision(
                update_available=False,
                reason=UpdateReason.OTA_DISABLED,
                device_id=device.id,
                known_version=known_version,
                known_version_source=device.known_firmware_source,
                policy_id=policy.id,
                policy_scope=policy.scope,
            )

        published = self._published(device.device_type_id)
        candidates = self._narrow(published, policy)
        if not candidates:
            # Distinguish "this type publishes nothing" from "a policy removed
            # everything it publishes": the first is a catalog state, the second
            # is an administrator's decision, and they need different actions.
            blocked = bool(published) and policy is not None
            return UpdateDecision(
                update_available=False,
                reason=UpdateReason.POLICY_BLOCKED if blocked else UpdateReason.NO_ELIGIBLE_RELEASE,
                device_id=device.id,
                known_version=known_version,
                known_version_source=device.known_firmware_source,
                policy_id=resolution.policy_id,
                policy_scope=resolution.scope,
            )

        release = max(candidates, key=lambda row: parse_version(row.version))
        if known_version is not None and parse_version(known_version) >= parse_version(
            release.version
        ):
            return UpdateDecision(
                update_available=False,
                reason=UpdateReason.NO_UPDATE,
                device_id=device.id,
                known_version=known_version,
                known_version_source=device.known_firmware_source,
                release=release,
                policy_id=resolution.policy_id,
                policy_scope=resolution.scope,
            )
        return UpdateDecision(
            update_available=True,
            reason=self._reason_for(policy),
            device_id=device.id,
            known_version=known_version,
            known_version_source=device.known_firmware_source,
            release=release,
            policy_id=resolution.policy_id,
            policy_scope=resolution.scope,
        )

    def allows_delivery(self, device: Device, release: FirmwareRelease) -> bool:
        """Whether the governing policy still permits handing out this version.

        Policy is normally an *offer* gate, and a signed manifest stays valid for
        its exact URL so an in-flight download is not broken by an unrelated
        change. A policy that blocks a version outright is different: the binary
        is what an install needs, so withholding it is the only way the block
        actually takes effect. Same resolver, same precedence rules.
        """
        resolution = self._policies.resolve_for_device(device)
        if resolution.conflict:
            logger.error(
                "policy_conflict_at_decision_time",
                extra={"extra_fields": {"device_id": device.id, "surface": "download"}},
            )
            return False
        policy = resolution.policy
        if policy is None:
            return True
        if policy.policy_type is PolicyType.DISABLE:
            return False
        if policy.policy_type is PolicyType.PIN:
            return release.version == policy.target_version
        return version_in_range(
            release.version, min_version=policy.min_version, max_version=policy.max_version
        )

    # --- internals ----------------------------------------------------------------

    def _published(self, device_type_id: int) -> list[FirmwareRelease]:
        """Published releases of this device type, dropping unparseable versions.

        A published release with a version the device could not compare would
        otherwise break every poll for that device type, so it is skipped and
        logged loudly instead.
        """
        usable: list[FirmwareRelease] = []
        for release in self._firmware.published_releases(device_type_id):
            try:
                parse_version(release.version)
            except ValueError:
                logger.error(
                    "published_release_has_invalid_version",
                    extra={"extra_fields": {"release_id": release.id}},
                )
                continue
            usable.append(release)
        return usable

    @staticmethod
    def _narrow(
        published: list[FirmwareRelease], policy: UpdatePolicy | None
    ) -> list[FirmwareRelease]:
        """Apply a policy's version restriction to the published candidate set."""
        if policy is None:
            return published
        if policy.policy_type is PolicyType.PIN:
            return [row for row in published if row.version == policy.target_version]
        if policy.policy_type is PolicyType.RANGE:
            return [
                row
                for row in published
                if version_in_range(
                    row.version, min_version=policy.min_version, max_version=policy.max_version
                )
            ]
        return []

    @staticmethod
    def _reason_for(policy: UpdatePolicy | None) -> UpdateReason:
        if policy is None:
            return UpdateReason.LATEST_AVAILABLE
        if policy.policy_type is PolicyType.PIN:
            return UpdateReason.PINNED_VERSION
        return UpdateReason.VERSION_RANGE

    @staticmethod
    def _known_version(device: Device) -> str | None:
        """Treat an unparseable administratively asserted version as unknown.

        The OTA request cannot report a version, so this value is asserted by an
        operator and is not format-enforced at the column level. A bad value must
        degrade to "unknown" rather than failing every poll for that device.
        """
        version = device.current_firmware_version
        if version is None:
            return None
        try:
            parse_version(version)
        except ValueError:
            logger.warning(
                "known_firmware_version_is_not_parseable",
                extra={"extra_fields": {"device_id": device.id}},
            )
            return None
        return version
