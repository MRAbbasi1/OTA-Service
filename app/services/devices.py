from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String, func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Device,
    DeviceStatus,
    DeviceType,
    KnownFirmwareSource,
    UpdateAttempt,
)
from app.domain.errors import DomainError
from app.domain.update_path import validate_device_type_path
from app.domain.version import parse_version


class DeviceError(DomainError):
    """Device domain error carrying a machine-readable code."""


def normalize_mac(mac: str) -> str:
    """Normalize to uppercase colon-separated XX:XX:XX:XX:XX:XX."""
    cleaned = mac.replace("-", "").replace(":", "").strip().upper()
    if len(cleaned) != 12:
        raise DeviceError("invalid_mac")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


class DeviceService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_type(
        self,
        code: str,
        name: str,
        platform: str,
        hardware_revision: str | None = None,
        description: str | None = None,
    ) -> DeviceType:
        # `code` and `platform` become path segments of every OTA URL for this
        # type, so they are validated and budgeted before the row exists.
        try:
            validate_device_type_path(code, platform)
        except DomainError as exc:
            raise DeviceError(exc.code) from None
        exists = self._session.execute(
            select(DeviceType).where(DeviceType.code == code)
        ).scalar_one_or_none()
        if exists is not None:
            raise DeviceError("device_type_exists")
        device_type = DeviceType(
            code=code,
            name=name,
            platform=platform,
            hardware_revision=hardware_revision,
            description=description,
        )
        self._session.add(device_type)
        self._session.flush()
        return device_type

    def list_types(self) -> list[DeviceType]:
        return list(self._session.execute(select(DeviceType).order_by(DeviceType.id)).scalars())

    def get_type(self, type_id: int) -> DeviceType:
        device_type = self._session.get(DeviceType, type_id)
        if device_type is None:
            raise DeviceError("device_type_not_found")
        return device_type

    def register(
        self,
        device_type_id: int,
        serial_number: int,
        raw_efuse_mac: str,
        network_mac: str | None = None,
        ota_enabled: bool = True,
    ) -> Device:
        self.get_type(device_type_id)  # must exist

        mac_normalized = normalize_mac(raw_efuse_mac)
        serial_taken = self._session.execute(
            select(Device).where(Device.serial_number == serial_number)
        ).scalar_one_or_none()
        if serial_taken is not None:
            raise DeviceError("serial_exists")
        mac_taken = self._session.execute(
            select(Device).where(Device.raw_efuse_mac == mac_normalized)
        ).scalar_one_or_none()
        if mac_taken is not None:
            raise DeviceError("mac_exists")

        device = Device(
            device_type_id=device_type_id,
            serial_number=serial_number,
            raw_efuse_mac=mac_normalized,
            network_mac=normalize_mac(network_mac) if network_mac else None,
        )
        self._session.add(device)
        self._session.flush()
        return device

    def get(self, device_id: int) -> Device:
        device = self._session.get(Device, device_id)
        if device is None:
            raise DeviceError("device_not_found")
        return device

    def set_status(self, device_id: int, status: DeviceStatus) -> Device:
        device = self.get(device_id)
        device.status = status
        self._session.flush()
        return device

    def set_ota_enabled(self, device_id: int, ota_enabled: bool) -> Device:
        device = self.get(device_id)
        device.ota_enabled = ota_enabled
        self._session.flush()
        return device

    def set_known_firmware_version(self, device_id: int, version: str | None) -> Device:
        """Record the version an administrator asserts the device is running.

        The OTA request carries no running version, so this is the only source of
        a known version today, and it is stored as ``ADMIN_ASSERTED`` rather than
        presented as an observed fact. Passing ``None`` clears it, which makes the
        device eligible for the latest release again. A served download never
        writes this field (``docs/15-implementation-decisions.md`` §5).
        """
        device = self.get(device_id)
        if version is None:
            device.current_firmware_version = None
            device.known_firmware_source = None
            device.known_firmware_observed_at = None
        else:
            try:
                parse_version(version)
            except ValueError:
                raise DeviceError("invalid_version") from None
            device.current_firmware_version = version
            device.known_firmware_source = KnownFirmwareSource.ADMIN_ASSERTED.value
            device.known_firmware_observed_at = datetime.now(UTC)
        self._session.flush()
        return device

    def list_update_attempts(self, device_id: int, limit: int = 20) -> list[UpdateAttempt]:
        """Server-observed OTA events for a device, newest first.

        These are events this server witnessed (an offer, a delivered download);
        none of them is evidence that a firmware booted successfully.
        """
        self.get(device_id)
        return list(
            self._session.execute(
                select(UpdateAttempt)
                .where(UpdateAttempt.device_id == device_id)
                .order_by(UpdateAttempt.id.desc())
                .limit(limit)
            ).scalars()
        )

    def list_devices(
        self,
        status: DeviceStatus | None = None,
        device_type_id: int | None = None,
        ota_enabled: bool | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[Device], int]:
        conditions = []
        if status is not None:
            conditions.append(Device.status == status)
        if device_type_id is not None:
            conditions.append(Device.device_type_id == device_type_id)
        if ota_enabled is not None:
            conditions.append(Device.ota_enabled.is_(ota_enabled))
        if search:
            conditions.append(Device.serial_number.cast(String).contains(search.strip()))
        stmt = select(Device)
        if conditions:
            stmt = stmt.where(*conditions)
        total = self._session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        rows = self._session.execute(
            stmt.options(selectinload(Device.device_type))
            .order_by(Device.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
        return list(rows), int(total)
