"""Device-facing OTA protocol.

This service owns the wire status codes on purpose: they are part of the firmware
contract rather than an HTTP detail of the admin API, because ``401`` and ``403``
drive different behaviour on the device (a 3-strike grace period versus an
immediate 24-hour lockout). The route layer stays a thin adapter.

Status matrix (``docs/15-implementation-decisions.md`` §4):

```text
401  invalid or missing credentials   -> 3-strike grace, then 24h lockout
403  unknown serial, MAC mismatch, or a disabled/retired device
                                      -> immediate 24h lockout
404  no offer: path identity mismatch, inactive device, OTA disabled,
     no eligible release, or nothing newer than the known version
                                      -> no retry, no lockout, next poll
429  rate limited                     -> skipped poll, no lockout
5xx  transient backend failure        -> device retries with backoff
```

The manifest endpoint serves the **stored** signed bytes verbatim. It never
re-renders or re-signs a manifest, so what is audited is literally what the
device received.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Device,
    DeviceType,
    FirmwareRelease,
    UpdateAttempt,
    UpdateAttemptStatus,
)
from app.domain.errors import DomainError
from app.domain.update_path import validate_filename, validate_version
from app.services.device_auth import AuthError, DeviceAuthService
from app.services.firmware import FirmwareService
from app.services.rate_limit import (
    FIRMWARE_RATE_LIMIT,
    FIRMWARE_SURFACE,
    MANIFEST_RATE_LIMIT,
    MANIFEST_SURFACE,
    InMemoryRateLimiter,
)
from app.services.update_decision import UpdateDecisionService, UpdateReason
from app.storage.base import ObjectStorage, ObjectStream

logger = logging.getLogger(__name__)

MANIFEST_CONTENT_TYPE = "application/json"
ARTIFACT_CONTENT_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class OtaResponse:
    """An OTA response prepared for writing by the route layer."""

    status_code: int
    body: bytes = b""
    content_type: str = MANIFEST_CONTENT_TYPE
    reason: str | None = None
    release_version: str | None = None
    stream: ObjectStream | None = None
    content_length: int | None = None
    on_stream_complete: Callable[[], None] | None = None


class OtaService:
    def __init__(
        self,
        session: Session,
        storage: ObjectStorage,
        firmware: FirmwareService,
        decision: UpdateDecisionService,
        limiter: InMemoryRateLimiter,
    ) -> None:
        self._session = session
        self._storage = storage
        self._firmware = firmware
        self._decision = decision
        self._limiter = limiter
        self._auth = DeviceAuthService(session)

    # --- manifest -----------------------------------------------------------------

    def manifest(
        self,
        *,
        device_type: str,
        platform: str,
        serial: str | None,
        mac: str | None,
        token: str | None,
    ) -> OtaResponse:
        try:
            device = self._auth.authenticate(serial, mac, token)
        except AuthError as exc:
            return self._auth_failure(exc)

        limited = self._rate_limited(device, MANIFEST_SURFACE, MANIFEST_RATE_LIMIT)
        if limited is not None:
            return limited

        decision = self._decision.decide_for_device(
            device, requested_code=device_type, requested_platform=platform
        )
        now = datetime.now(UTC)
        device.last_manifest_check_at = now

        release = decision.release
        if not decision.update_available or release is None:
            self._session.commit()
            logger.info(
                "manifest_no_offer",
                extra={
                    "extra_fields": {
                        "device_id": device.id,
                        "device_serial": device.serial_number,
                        "reason": decision.reason.value,
                    }
                },
            )
            return self._no_offer(decision.reason)

        manifest = release.manifest
        if manifest is None:
            # A published release always has a manifest (publishing requires
            # one); its absence is a server-side integrity failure, never a 4xx.
            logger.error(
                "published_release_has_no_manifest",
                extra={"extra_fields": {"release_id": release.id}},
            )
            return self._transient_failure("manifest_missing", device)

        raw = self._read_object(manifest.storage_key, device)
        if raw is None:
            return self._transient_failure("storage_unavailable", device)

        self._record_offer(device, release)
        self._session.commit()
        logger.info(
            "manifest_served",
            extra={
                "extra_fields": {
                    "device_id": device.id,
                    "device_serial": device.serial_number,
                    "release_id": release.id,
                    "version": release.version,
                    "bytes": len(raw),
                }
            },
        )
        return OtaResponse(
            status_code=status.HTTP_200_OK,
            body=raw,
            content_type=MANIFEST_CONTENT_TYPE,
            release_version=release.version,
        )

    # --- firmware -----------------------------------------------------------------

    def firmware(
        self,
        *,
        device_type: str,
        platform: str,
        version: str,
        filename: str,
        serial: str | None,
        mac: str | None,
        token: str | None,
    ) -> OtaResponse:
        try:
            device = self._auth.authenticate(serial, mac, token)
        except AuthError as exc:
            return self._auth_failure(exc)

        limited = self._rate_limited(device, FIRMWARE_SURFACE, FIRMWARE_RATE_LIMIT)
        if limited is not None:
            return limited

        reason = self._decision.check_eligibility(
            device, requested_code=device_type, requested_platform=platform
        )
        if reason is not None:
            self._session.commit()
            logger.info(
                "firmware_not_entitled",
                extra={
                    "extra_fields": {
                        "device_id": device.id,
                        "device_serial": device.serial_number,
                        "reason": reason.value,
                    }
                },
            )
            return self._no_offer(reason)

        release = self._resolve_release(device, version=version, filename=filename)
        if release is None:
            self._session.commit()
            logger.info(
                "firmware_release_not_found",
                extra={
                    "extra_fields": {
                        "device_id": device.id,
                        "device_serial": device.serial_number,
                        "version": version,
                        "filename": filename,
                    }
                },
            )
            return self._no_offer(UpdateReason.NO_ELIGIBLE_RELEASE, reason_code="RELEASE_NOT_FOUND")

        if not self._decision.allows_delivery(device, release):
            # A policy that blocks this version must block the bytes too: an offer
            # withheld at poll time would otherwise still be installable from a
            # manifest the device already holds.
            self._session.commit()
            logger.info(
                "firmware_blocked_by_policy",
                extra={
                    "extra_fields": {
                        "device_id": device.id,
                        "device_serial": device.serial_number,
                        "version": release.version,
                    }
                },
            )
            return self._no_offer(UpdateReason.POLICY_BLOCKED)

        artifact = release.artifact
        if artifact is None:
            logger.error(
                "published_release_has_no_artifact",
                extra={"extra_fields": {"release_id": release.id}},
            )
            return self._transient_failure("artifact_missing", device)

        try:
            stream = self._storage.open_stream(artifact.storage_key)
        except Exception:  # noqa: BLE001 - any object-store failure is a transient 5xx
            logger.exception(
                "storage_stream_open_failed",
                extra={
                    "extra_fields": {
                        "storage_key": artifact.storage_key,
                        "device_id": device.id,
                    }
                },
            )
            return self._transient_failure("storage_unavailable", device)
        if stream.size != artifact.size:
            actual_size = stream.size
            try:
                stream.close()
            except Exception:
                logger.exception(
                    "firmware_stream_close_failed",
                    extra={"extra_fields": {"storage_key": artifact.storage_key}},
                )
            logger.error(
                "stored_artifact_size_mismatch",
                extra={
                    "extra_fields": {
                        "release_id": release.id,
                        "expected": artifact.size,
                        "actual": actual_size,
                    }
                },
            )
            return self._transient_failure("artifact_size_mismatch", device)

        try:
            self._session.commit()
        except Exception:
            try:
                stream.close()
            except Exception:
                logger.exception(
                    "firmware_stream_close_failed",
                    extra={"extra_fields": {"storage_key": artifact.storage_key}},
                )
            raise
        return OtaResponse(
            status_code=status.HTTP_200_OK,
            content_type=ARTIFACT_CONTENT_TYPE,
            release_version=release.version,
            stream=stream,
            content_length=artifact.size,
            on_stream_complete=self._completion_callback(
                device.id, release.id, release.version, artifact.size
            ),
        )

    # --- internals ----------------------------------------------------------------

    def _resolve_release(
        self, device: Device, *, version: str, filename: str
    ) -> FirmwareRelease | None:
        """Resolve the exact release the device was given a signed URL for.

        Only a published release of the device's own device type, with a matching
        artifact filename, can be served. Any other request is a no-offer
        response, so a signed manifest is never a licence to pull arbitrary
        published binaries.
        """
        try:
            validate_version(version)
            validate_filename(filename)
        except DomainError:
            return None
        device_type = self._session.get(DeviceType, device.device_type_id)
        if device_type is None:
            return None
        release = self._firmware.published_by_version(device_type.id, version)
        if release is None or release.artifact is None:
            return None
        if release.artifact.filename != filename:
            return None
        return release

    def _rate_limited(
        self,
        device: Device,
        surface: str,
        limit: object,
    ) -> OtaResponse | None:
        if self._limiter.allow(f"{surface}:{device.id}", limit):  # type: ignore[arg-type]
            return None
        self._session.commit()  # the device was seen, even though it was limited
        logger.warning(
            "device_rate_limited",
            extra={
                "extra_fields": {
                    "device_id": device.id,
                    "device_serial": device.serial_number,
                    "surface": surface,
                }
            },
        )
        return OtaResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            body=_json({"code": "rate_limited"}),
            reason="RATE_LIMITED",
        )

    def _auth_failure(self, exc: AuthError) -> OtaResponse:
        """Generic body, specific reason logged.

        The response body never distinguishes an unknown serial from a MAC
        mismatch, so it cannot be used to enumerate enrolled devices; operators
        get the detail from the log.
        """
        if exc.http_status == status.HTTP_401_UNAUTHORIZED:
            logger.info("device_auth_failed", extra={"extra_fields": {"code": exc.code}})
            return OtaResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                body=_json({"code": "unauthorized"}),
                reason=exc.code,
            )
        logger.warning("device_auth_forbidden", extra={"extra_fields": {"code": exc.code}})
        return OtaResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            body=_json({"code": "forbidden"}),
            reason=exc.code,
        )

    def _no_offer(self, reason: UpdateReason, *, reason_code: str | None = None) -> OtaResponse:
        label = reason_code or reason.value
        payload = {"code": "no_offer", "reason": label}
        return OtaResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            body=_json(payload),
            reason=label,
        )

    def _transient_failure(self, code: str, device: Device) -> OtaResponse:
        logger.error(
            "ota_transient_failure",
            extra={"extra_fields": {"code": code, "device_id": device.id}},
        )
        payload = {"code": "unavailable"}
        return OtaResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            body=_json(payload),
            reason=code.upper(),
        )

    def _read_object(self, storage_key: str, device: Device) -> bytes | None:
        try:
            return self._storage.get_bytes(storage_key)
        except Exception:  # noqa: BLE001 - any storage failure is a transient 5xx
            logger.error(
                "storage_read_failed",
                extra={
                    "extra_fields": {
                        "storage_key": storage_key,
                        "device_id": device.id,
                    }
                },
            )
            return None

    def _record_offer(self, device: Device, release: FirmwareRelease) -> None:
        """Record one offer per device and target version.

        Recording every poll would add a row stream per device for no extra
        information: ``last_manifest_check_at`` already records that the device
        checked, and the offer only changes when a different version is offered.
        """
        existing = self._session.execute(
            select(UpdateAttempt.id)
            .where(
                UpdateAttempt.device_id == device.id,
                UpdateAttempt.to_version == release.version,
                UpdateAttempt.status == UpdateAttemptStatus.UPDATE_OFFERED,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return
        self._session.add(
            UpdateAttempt(
                device_id=device.id,
                release_id=release.id,
                from_version=device.current_firmware_version,
                from_version_source=device.known_firmware_source,
                to_version=release.version,
                status=UpdateAttemptStatus.UPDATE_OFFERED,
            )
        )

    def _completion_callback(
        self, device_id: int, release_id: int, version: str, size: int
    ) -> Callable[[], None]:
        factory = sessionmaker(
            bind=self._session.get_bind(),
            autoflush=False,
            expire_on_commit=False,
        )

        def record_completed_download() -> None:
            try:
                with factory.begin() as session:
                    device = session.get(Device, device_id)
                    release = session.get(FirmwareRelease, release_id)
                    if device is None or release is None:
                        logger.error(
                            "firmware_download_completion_record_missing",
                            extra={
                                "extra_fields": {
                                    "device_id": device_id,
                                    "release_id": release_id,
                                }
                            },
                        )
                        return
                    _record_download(session, device, release, served=size)
                logger.info(
                    "firmware_download_served",
                    extra={
                        "extra_fields": {
                            "device_id": device_id,
                            "release_id": release_id,
                            "version": version,
                            "bytes": size,
                        }
                    },
                )
            except Exception:  # noqa: BLE001 - response is already fully delivered
                logger.exception(
                    "firmware_download_completion_record_failed",
                    extra={"extra_fields": {"device_id": device_id, "release_id": release_id}},
                )

        return record_completed_download


def _record_download(
    session: Session, device: Device, release: FirmwareRelease, *, served: int
) -> None:
    """Record a transfer only after every expected byte was emitted."""
    now = datetime.now(UTC)
    device.last_update_started_at = now
    device.last_update_completed_at = now
    device.last_download_served_version = release.version
    device.last_download_served_at = now
    session.add(
        UpdateAttempt(
            device_id=device.id,
            release_id=release.id,
            from_version=device.current_firmware_version,
            from_version_source=device.known_firmware_source,
            to_version=release.version,
            status=UpdateAttemptStatus.DOWNLOAD_SERVED,
            bytes_served=served,
        )
    )


def _json(payload: dict[str, str]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()
