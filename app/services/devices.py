from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import String, asc, desc, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Device,
    DeviceCommunication,
    DeviceStatus,
    DeviceToken,
    DeviceType,
    FirmwareRelease,
    KnownFirmwareSource,
    UpdateAttempt,
    UpdatePolicy,
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

    def list_types(
        self,
        *,
        search: str | None = None,
        is_active: bool | None = None,
        sort: str = "name",
        order: str = "asc",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[DeviceType], int]:
        conditions = []
        if search:
            term = f"%{search.strip()}%"
            conditions.append(
                or_(
                    DeviceType.name.ilike(term),
                    DeviceType.code.ilike(term),
                    DeviceType.platform.ilike(term),
                )
            )
        if is_active is not None:
            conditions.append(DeviceType.is_active.is_(is_active))
        stmt = select(DeviceType)
        if conditions:
            stmt = stmt.where(*conditions)
        total = self._session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        columns = {
            "name": DeviceType.name,
            "code": DeviceType.code,
            "platform": DeviceType.platform,
            "created_at": DeviceType.created_at,
        }
        direction = desc if order == "desc" else asc
        rows = self._session.execute(
            stmt.order_by(direction(columns[sort]), DeviceType.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
        return list(rows), int(total)

    def get_type(self, type_id: int) -> DeviceType:
        device_type = self._session.get(DeviceType, type_id)
        if device_type is None:
            raise DeviceError("device_type_not_found")
        return device_type

    def update_type(self, type_id: int, changes: dict[str, object]) -> DeviceType:
        device_type = self.get_type(type_id)
        identity_changed = any(
            field in changes and changes[field] != getattr(device_type, field)
            for field in ("code", "platform")
        )
        if identity_changed and self._type_has_dependencies(type_id):
            raise DeviceError("device_type_identity_locked")
        code = str(changes.get("code", device_type.code))
        platform = str(changes.get("platform", device_type.platform))
        try:
            validate_device_type_path(code, platform)
        except DomainError as exc:
            raise DeviceError(exc.code) from None
        if code != device_type.code:
            existing = self._session.execute(
                select(DeviceType.id).where(DeviceType.code == code, DeviceType.id != type_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise DeviceError("device_type_exists")
        for field, value in changes.items():
            setattr(device_type, field, value)
        self._session.flush()
        return device_type

    def set_type_active(self, type_id: int, is_active: bool) -> DeviceType:
        device_type = self.get_type(type_id)
        device_type.is_active = is_active
        self._session.flush()
        return device_type

    def delete_type(self, type_id: int) -> None:
        device_type = self.get_type(type_id)
        if self._type_has_dependencies(type_id):
            raise DeviceError("device_type_has_dependencies")
        self._session.delete(device_type)
        self._session.flush()

    def deletable_type_ids(self, type_ids: list[int]) -> set[int]:
        """Return types with no retained device, release, or policy reference."""
        candidates = set(type_ids)
        if not candidates:
            return set()
        blocked: set[int] = set()
        blocked.update(
            self._session.execute(
                select(Device.device_type_id).where(Device.device_type_id.in_(candidates))
            ).scalars()
        )
        blocked.update(
            self._session.execute(
                select(FirmwareRelease.device_type_id).where(
                    FirmwareRelease.device_type_id.in_(candidates)
                )
            ).scalars()
        )
        blocked.update(
            type_id
            for type_id in self._session.execute(
                select(UpdatePolicy.device_type_id).where(
                    UpdatePolicy.device_type_id.in_(candidates)
                )
            ).scalars()
            if type_id is not None
        )
        return candidates - blocked

    def register(
        self,
        device_type_id: int,
        serial_number: int,
        raw_efuse_mac: str | None = None,
        network_mac: str | None = None,
        ota_enabled: bool = True,
    ) -> Device:
        device_type = self.get_type(device_type_id)
        if not device_type.is_active:
            raise DeviceError("device_type_inactive")

        mac_normalized = normalize_mac(raw_efuse_mac.strip()) if raw_efuse_mac and raw_efuse_mac.strip() else None
        serial_taken = self._session.execute(
            select(Device).where(Device.serial_number == serial_number)
        ).scalar_one_or_none()
        if serial_taken is not None:
            raise DeviceError("serial_exists")
        if mac_normalized is not None:
            mac_taken = self._session.execute(
                select(Device).where(Device.raw_efuse_mac == mac_normalized)
            ).scalar_one_or_none()
            if mac_taken is not None:
                raise DeviceError("mac_exists")

        device = Device(
            device_type_id=device_type_id,
            serial_number=serial_number,
            raw_efuse_mac=mac_normalized,
            network_mac=normalize_mac(network_mac.strip()) if network_mac and network_mac.strip() else None,
            ota_enabled=ota_enabled,
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
        allowed = {
            DeviceStatus.PROVISIONED: {
                DeviceStatus.ACTIVE,
                DeviceStatus.DISABLED,
                DeviceStatus.RETIRED,
            },
            DeviceStatus.ACTIVE: {DeviceStatus.DISABLED, DeviceStatus.RETIRED},
            DeviceStatus.DISABLED: {DeviceStatus.ACTIVE, DeviceStatus.RETIRED},
            # Restoration is deliberately conservative: a retained device
            # returns disabled, then requires explicit activation and OTA
            # enablement before it can receive an update again.
            DeviceStatus.RETIRED: {DeviceStatus.DISABLED},
        }
        if status is not device.status and status not in allowed[device.status]:
            raise DeviceError("invalid_device_transition")
        device.status = status
        if status is DeviceStatus.RETIRED:
            device.ota_enabled = False
        self._session.flush()
        return device

    def update_identity(self, device_id: int, changes: dict[str, object]) -> Device:
        device = self.get(device_id)
        if device.status is not DeviceStatus.PROVISIONED or self._device_has_history(device_id):
            raise DeviceError("device_identity_locked")
        if "device_type_id" in changes:
            device_type = self.get_type(cast(int, changes["device_type_id"]))
            if not device_type.is_active:
                raise DeviceError("device_type_inactive")
        if "serial_number" in changes:
            serial = cast(int, changes["serial_number"])
            existing = self._session.execute(
                select(Device.id).where(Device.serial_number == serial, Device.id != device_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise DeviceError("serial_exists")
        if "raw_efuse_mac" in changes:
            raw_val = changes["raw_efuse_mac"]
            if raw_val and str(raw_val).strip():
                mac = normalize_mac(str(raw_val).strip())
                existing = self._session.execute(
                    select(Device.id).where(Device.raw_efuse_mac == mac, Device.id != device_id)
                ).scalar_one_or_none()
                if existing is not None:
                    raise DeviceError("mac_exists")
                changes["raw_efuse_mac"] = mac
            else:
                changes["raw_efuse_mac"] = None
        for field, value in changes.items():
            setattr(device, field, value)
        self._session.flush()
        return device

    def purge(self, device_id: int) -> None:
        """Remove a mistaken enrollment and its owned token credentials."""
        device = self.get(device_id)
        if device.status is not DeviceStatus.PROVISIONED or self._device_has_purge_blockers(
            device_id
        ):
            raise DeviceError("device_has_dependencies")
        self._session.delete(device)
        self._session.flush()

    def device_action_eligibility(self, devices: list[Device]) -> dict[int, tuple[bool, bool]]:
        """Return ``can_update_identity`` and ``can_purge`` for each device."""
        provisioned = {device.id for device in devices if device.status is DeviceStatus.PROVISIONED}
        if not provisioned:
            return {device.id: (False, False) for device in devices}

        token_ids = set(
            self._session.execute(
                select(DeviceToken.device_id).where(DeviceToken.device_id.in_(provisioned))
            ).scalars()
        )
        attempt_ids = set(
            self._session.execute(
                select(UpdateAttempt.device_id).where(UpdateAttempt.device_id.in_(provisioned))
            ).scalars()
        )
        policy_ids = set(
            self._session.execute(
                select(UpdatePolicy.device_id).where(UpdatePolicy.device_id.in_(provisioned))
            ).scalars()
        )
        communication_ids = set(
            self._session.scalars(
                select(DeviceCommunication.device_id).where(
                    DeviceCommunication.device_id.in_(provisioned)
                )
            )
        )
        history = token_ids | attempt_ids | policy_ids | communication_ids
        purge_blockers = attempt_ids | policy_ids | communication_ids
        return {
            device.id: (
                device.id in provisioned and device.id not in history,
                device.id in provisioned and device.id not in purge_blockers,
            )
            for device in devices
        }

    def _type_has_dependencies(self, type_id: int) -> bool:
        checks = (
            select(Device.id).where(Device.device_type_id == type_id).limit(1),
            select(FirmwareRelease.id).where(FirmwareRelease.device_type_id == type_id).limit(1),
            select(UpdatePolicy.id).where(UpdatePolicy.device_type_id == type_id).limit(1),
        )
        return any(self._session.execute(stmt).scalar_one_or_none() is not None for stmt in checks)

    def _device_has_history(self, device_id: int) -> bool:
        checks = (
            select(DeviceCommunication.id)
            .where(DeviceCommunication.device_id == device_id)
            .limit(1),
            select(DeviceToken.id).where(DeviceToken.device_id == device_id).limit(1),
            select(UpdateAttempt.id).where(UpdateAttempt.device_id == device_id).limit(1),
            select(UpdatePolicy.id).where(UpdatePolicy.device_id == device_id).limit(1),
        )
        return any(self._session.execute(stmt).scalar_one_or_none() is not None for stmt in checks)

    def _device_has_purge_blockers(self, device_id: int) -> bool:
        """Return whether retained operational history references the device.

        Token rows are credentials owned by the device record and are deleted
        with it. Update attempts and policies remain retained history and block
        the exceptional purge.
        """
        checks = (
            select(DeviceCommunication.id)
            .where(DeviceCommunication.device_id == device_id)
            .limit(1),
            select(UpdateAttempt.id).where(UpdateAttempt.device_id == device_id).limit(1),
            select(UpdatePolicy.id).where(UpdatePolicy.device_id == device_id).limit(1),
        )
        return any(self._session.execute(stmt).scalar_one_or_none() is not None for stmt in checks)

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
        sort: str = "serial_number",
        order: str = "asc",
    ) -> tuple[list[Device], int]:
        conditions = []
        if status is not None:
            conditions.append(Device.status == status)
        if device_type_id is not None:
            conditions.append(Device.device_type_id == device_type_id)
        if ota_enabled is not None:
            conditions.append(Device.ota_enabled.is_(ota_enabled))
        if search:
            term = search.strip()
            conditions.append(
                or_(
                    Device.serial_number.cast(String).contains(term),
                    Device.raw_efuse_mac.ilike(f"%{term}%"),
                )
            )
        stmt = select(Device)
        if conditions:
            stmt = stmt.where(*conditions)
        total = self._session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        columns = {
            "serial_number": Device.serial_number,
            "status": Device.status,
            "registered_at": Device.registered_at,
            "last_seen_at": Device.last_seen_at,
        }
        direction = desc if order == "desc" else asc
        rows = self._session.execute(
            stmt.options(selectinload(Device.device_type))
            .order_by(direction(columns[sort]), Device.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
        return list(rows), int(total)
