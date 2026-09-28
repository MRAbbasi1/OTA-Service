"""Fleet views for the dashboard.

Every number here is derived from what the server actually observed. Nothing is
inferred that the request contract cannot support: there is no "installed
successfully" count, because no device reports a boot
(`docs/15-implementation-decisions.md` §5), and the firmware distribution counts
*known* versions with their provenance rather than presenting an assertion as an
observation.

"Needs attention" is a small, explicit, documented set of categories. Each is a
statement about the platform's own view of the device, not a guess about the
field:

```text
NEVER_SEEN              registered, never authenticated, more than a day ago
STALE_CHECK             active but no manifest poll in a week
DOWNLOAD_UNCONFIRMED    a binary was served days ago and the known version is
                        still not that version, which is the closest
                        server-observable signal to "the update did not take"
```

The third category is the honest version of "failed update": the device never
reports post-reboot success, so the platform can only say that it handed out a
version and that what it knows about the device disagrees.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import (
    Device,
    DeviceStatus,
    DeviceType,
    FirmwareRelease,
    FirmwareReleaseStatus,
    UpdateAttempt,
    UpdateAttemptStatus,
    UpdatePolicy,
)

NEVER_SEEN_AFTER = timedelta(days=1)
STALE_CHECK_AFTER = timedelta(days=7)
DOWNLOAD_UNCONFIRMED_AFTER = timedelta(days=2)


class AttentionCategory(enum.StrEnum):
    NEVER_SEEN = "NEVER_SEEN"
    STALE_CHECK = "STALE_CHECK"
    DOWNLOAD_UNCONFIRMED = "DOWNLOAD_UNCONFIRMED"


ATTENTION_EXPLANATIONS: dict[AttentionCategory, str] = {
    AttentionCategory.NEVER_SEEN: (
        "Registered more than a day ago and has never authenticated; the device may "
        "not be deployed, provisioned, or able to reach the OTA host."
    ),
    AttentionCategory.STALE_CHECK: (
        "Active device with no manifest check in the last week; it is not polling."
    ),
    AttentionCategory.DOWNLOAD_UNCONFIRMED: (
        "A firmware binary was served more than two days ago and the known version "
        "still is not that version. The device never reports a successful boot, so "
        "this is the closest server-observable signal that the update did not take."
    ),
}


@dataclass(frozen=True)
class FleetSummary:
    total_devices: int
    active_devices: int
    provisioned_devices: int
    disabled_devices: int
    retired_devices: int
    ota_disabled_devices: int
    total_device_types: int
    published_releases: int
    active_policies: int
    checks_last_24h: int
    offers_last_24h: int
    downloads_last_24h: int
    devices_needing_attention: int
    generated_at: datetime


@dataclass(frozen=True)
class DistributionRow:
    device_type_code: str
    platform: str
    version: str | None
    version_source: str | None
    device_count: int


@dataclass(frozen=True)
class ActivityRow:
    id: int
    device_id: int
    device_serial: int
    device_type_code: str
    status: UpdateAttemptStatus
    from_version: str | None
    to_version: str | None
    bytes_served: int | None
    created_at: datetime


def attention_conditions(now: datetime) -> dict[AttentionCategory, list[ColumnElement[bool]]]:
    """The SQL conditions behind each attention category, in one place."""
    return {
        AttentionCategory.NEVER_SEEN: [
            Device.last_seen_at.is_(None),
            Device.registered_at < now - NEVER_SEEN_AFTER,
        ],
        AttentionCategory.STALE_CHECK: [
            Device.status == DeviceStatus.ACTIVE,
            Device.last_manifest_check_at.is_(None)
            | (Device.last_manifest_check_at < now - STALE_CHECK_AFTER),
        ],
        AttentionCategory.DOWNLOAD_UNCONFIRMED: [
            Device.last_download_served_at.is_not(None),
            Device.last_download_served_at < now - DOWNLOAD_UNCONFIRMED_AFTER,
            Device.last_download_served_version.is_not(None),
            # String equality is the correct numeric-equality test on the canonical
            # `MAJOR.MINOR.PATCH` form, so no ordering is needed here.
            Device.current_firmware_version.is_distinct_from(Device.last_download_served_version),
        ],
    }


class DashboardService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def summary(self, *, now: datetime | None = None) -> FleetSummary:
        moment = now or datetime.now(UTC)
        since = moment - timedelta(hours=24)

        status_counts = dict(
            self._session.execute(
                select(Device.status, func.count()).select_from(Device).group_by(Device.status)
            ).all()
        )
        attempt_counts = dict(
            self._session.execute(
                select(UpdateAttempt.status, func.count())
                .select_from(UpdateAttempt)
                .where(UpdateAttempt.created_at >= since)
                .group_by(UpdateAttempt.status)
            ).all()
        )
        return FleetSummary(
            total_devices=sum(status_counts.values()),
            active_devices=status_counts.get(DeviceStatus.ACTIVE, 0),
            provisioned_devices=status_counts.get(DeviceStatus.PROVISIONED, 0),
            disabled_devices=status_counts.get(DeviceStatus.DISABLED, 0),
            retired_devices=status_counts.get(DeviceStatus.RETIRED, 0),
            ota_disabled_devices=self._count(
                select(func.count()).select_from(Device).where(Device.ota_enabled.is_(False))
            ),
            total_device_types=self._count(
                select(func.count()).select_from(DeviceType).where(DeviceType.is_active.is_(True))
            ),
            published_releases=self._count(
                select(func.count())
                .select_from(FirmwareRelease)
                .where(FirmwareRelease.status == FirmwareReleaseStatus.PUBLISHED)
            ),
            active_policies=self._count(
                select(func.count())
                .select_from(UpdatePolicy)
                .where(UpdatePolicy.is_active.is_(True))
            ),
            checks_last_24h=self._count(
                select(func.count())
                .select_from(Device)
                .where(Device.last_manifest_check_at >= since)
            ),
            offers_last_24h=attempt_counts.get(UpdateAttemptStatus.UPDATE_OFFERED, 0),
            downloads_last_24h=attempt_counts.get(UpdateAttemptStatus.DOWNLOAD_SERVED, 0),
            devices_needing_attention=self.count_attention_total(now=moment),
            generated_at=moment,
        )

    def firmware_distribution(self) -> list[DistributionRow]:
        """Known firmware versions per device type, including the unknown row.

        `known_firmware_version` is an assertion with a provenance, and a device
        whose version was never asserted appears as `version = null` instead of
        being silently folded into a bucket or dropped.
        """
        rows = self._session.execute(
            select(
                DeviceType.code,
                DeviceType.platform,
                Device.current_firmware_version,
                Device.known_firmware_source,
                func.count(),
            )
            .select_from(Device)
            .join(DeviceType, Device.device_type_id == DeviceType.id)
            .group_by(
                DeviceType.code,
                DeviceType.platform,
                Device.current_firmware_version,
                Device.known_firmware_source,
            )
            .order_by(DeviceType.code, Device.current_firmware_version)
        ).all()
        return [
            DistributionRow(
                device_type_code=code,
                platform=platform,
                version=version,
                version_source=source,
                device_count=int(count),
            )
            for code, platform, version, source, count in rows
        ]

    def update_activity(
        self, *, limit: int = 20, now: datetime | None = None
    ) -> tuple[list[ActivityRow], dict[str, int]]:
        moment = now or datetime.now(UTC)
        rows = self._session.execute(
            select(UpdateAttempt, Device.serial_number, DeviceType.code)
            .join(Device, UpdateAttempt.device_id == Device.id)
            .join(DeviceType, Device.device_type_id == DeviceType.id)
            .order_by(UpdateAttempt.id.desc())
            .limit(limit)
        ).all()
        items = [
            ActivityRow(
                id=attempt.id,
                device_id=attempt.device_id,
                device_serial=serial,
                device_type_code=code,
                status=attempt.status,
                from_version=attempt.from_version,
                to_version=attempt.to_version,
                bytes_served=attempt.bytes_served,
                created_at=attempt.created_at,
            )
            for attempt, serial, code in rows
        ]
        second = moment - timedelta(seconds=86400)
        week = moment - timedelta(days=7)
        counts = {
            "offers_last_24h": self._attempts_since(UpdateAttemptStatus.UPDATE_OFFERED, second),
            "downloads_last_24h": self._attempts_since(UpdateAttemptStatus.DOWNLOAD_SERVED, second),
            "offers_last_7d": self._attempts_since(UpdateAttemptStatus.UPDATE_OFFERED, week),
            "downloads_last_7d": self._attempts_since(UpdateAttemptStatus.DOWNLOAD_SERVED, week),
        }
        return items, counts

    def attention_counts(self, *, now: datetime | None = None) -> dict[AttentionCategory, int]:
        moment = now or datetime.now(UTC)
        counts: dict[AttentionCategory, int] = {}
        for category, conditions in attention_conditions(moment).items():
            counts[category] = self._count(
                select(func.count()).select_from(Device).where(*conditions)
            )
        return counts

    def count_attention_total(self, *, now: datetime | None = None) -> int:
        return self._count(
            select(func.count()).select_from(Device).where(_any_attention(now or datetime.now(UTC)))
        )

    def devices_needing_attention(
        self,
        category: AttentionCategory,
        *,
        page: int = 1,
        page_size: int = 50,
        now: datetime | None = None,
    ) -> tuple[list[Device], int]:
        moment = now or datetime.now(UTC)
        conditions = attention_conditions(moment)[category]
        stmt = select(Device).where(*conditions)
        total = self._count(select(func.count()).select_from(stmt.subquery()))
        rows = self._session.execute(
            stmt.options(selectinload(Device.device_type))
            .order_by(Device.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
        return list(rows), total

    def _attempts_since(self, status: UpdateAttemptStatus, since: datetime) -> int:
        return self._count(
            select(func.count())
            .select_from(UpdateAttempt)
            .where(UpdateAttempt.status == status, UpdateAttempt.created_at >= since)
        )

    def _count(self, statement: Select[Any]) -> int:
        return int(self._session.execute(statement).scalar_one())


def _any_attention(now: datetime) -> ColumnElement[bool]:
    """Any category: a device counted once even if it matches several."""
    return or_(*[and_(*conditions) for conditions in attention_conditions(now).values()])
