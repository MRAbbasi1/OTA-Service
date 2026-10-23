from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Device, DeviceStatus
from app.services.device_tokens import DeviceTokenService, hash_token
from app.services.devices import DeviceError, normalize_mac

# Firmware contract (docs/OTA-CONTRACT.md):
#   401 -> invalid authentication (device keeps a 3-strike grace period)
#   403 -> device definitively not entitled (immediate 24h device-side lockout)


class AuthError(Exception):
    def __init__(self, http_status: int, code: str) -> None:
        super().__init__(code)
        self.http_status = http_status
        self.code = code


@dataclass(frozen=True)
class DeviceCredentials:
    device: Device


class DeviceAuthService:
    """Validates X-Device-Serial / X-Device-Mac / X-Device-Token headers."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._tokens = DeviceTokenService(session)

    def authenticate(
        self,
        serial: str | None,
        mac: str | None,
        token: str | None,
    ) -> Device:
        serial_value = self._require(serial, "missing_serial")
        token_value = self._require(token, "missing_token")

        if not re.fullmatch(r"[0-9]{1,19}", serial_value.strip()) or int(serial_value) > 2**63 - 1:
            raise AuthError(status.HTTP_403_FORBIDDEN, "unknown_device")
        device = self._session.execute(
            select(Device).where(Device.serial_number == int(serial_value))
        ).scalar_one_or_none()
        if device is None:
            # Unknown device: positively refused (docs/05-device-management.md §6).
            raise AuthError(status.HTTP_403_FORBIDDEN, "unknown_device")

        # eFuse MAC validation is optional:
        # If omitted, or not configured on device, authentication succeeds via serial and token.
        # If provided, validate/normalize format, but any mismatch does NOT cause failure.
        if mac and mac.strip():
            try:
                normalize_mac(mac.strip())
            except DeviceError:
                pass

        if device.status in (DeviceStatus.DISABLED, DeviceStatus.RETIRED):
            raise AuthError(status.HTTP_403_FORBIDDEN, "device_disabled")

        record = self._tokens.find_active_by_hash(hash_token(token_value))
        if record is None or record.device_id != device.id:
            raise AuthError(status.HTTP_401_UNAUTHORIZED, "invalid_token")

        device.last_seen_at = datetime.now(UTC)
        self._session.flush()
        return device

    @staticmethod
    def _require(value: str | None, code: str) -> str:
        if not value or not value.strip():
            raise AuthError(status.HTTP_401_UNAUTHORIZED, code)
        return value
