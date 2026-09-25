"""Device-facing OTA endpoints.

The two routes the firmware itself calls, and nothing else:

```text
GET https://api.ota-service.example/api/v1/firmware/{device_type}/{platform}/manifest.json
GET https://cdn.ota-service.example/firmware/{device_type}/{platform}/v{version}/{filename}
```

Both hostnames are served by this application. The delivery hostname is not
MinIO and not a static file server, because the device contract requires the
device headers to be validated on the binary download too, and because MinIO must
stay internal (`AGENTS.md` §5, `docs/OtaManager.md` §10.7).

The routes are deliberately thin: they resolve dependencies, hand the request to
:class:`app.services.ota.OtaService`, and write the body with the exact
``Content-Length``. Firmware objects are streamed in bounded chunks; manifests
remain small stored byte responses. No OTA response is a redirect or cacheable,
and the status codes come from the service because they are part of the
firmware contract rather than an HTTP detail here.

Path params are validated by the service, never by FastAPI's `422`, so a bad
version or filename is a no-offer `404` instead of a non-retryable validation
error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    build_firmware_service,
    build_update_decision_service,
    get_manifest_verifier,
    get_rate_limiter,
    get_session,
    get_settings,
    get_storage,
)
from app.core.config import Settings
from app.domain.manifest import ManifestVerifier
from app.services.ota import OtaResponse, OtaService
from app.services.rate_limit import InMemoryRateLimiter
from app.storage.base import ObjectStorage

router = APIRouter(tags=["ota"])
logger = logging.getLogger(__name__)

SerialHeader = Annotated[str | None, Header(alias="X-Device-Serial")]
MacHeader = Annotated[str | None, Header(alias="X-Device-Mac")]
TokenHeader = Annotated[str | None, Header(alias="X-Device-Token")]

# A device neither caches nor follows a redirect, and every OTA response is
# generated for one authenticated device, so no OTA response may be stored by an
# intermediary.
_BASE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

# `X-OTA-Reason` is a non-contractual diagnostic header for field debugging. It
# is emitted only where the reason describes the caller itself, so it can never
# be used to probe whether some other serial number is enrolled: the 401/403
# paths stay generic in both the body and the headers.
_REASON_HEADER_STATUSES = {
    status.HTTP_404_NOT_FOUND,
    status.HTTP_429_TOO_MANY_REQUESTS,
    status.HTTP_503_SERVICE_UNAVAILABLE,
}

_DEVICE_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "Invalid or missing device credentials."},
    status.HTTP_403_FORBIDDEN: {
        "description": (
            "Unknown serial number, raw eFuse MAC mismatch, or a disabled or retired "
            "device. The device applies an immediate 24-hour lockout."
        )
    },
    status.HTTP_404_NOT_FOUND: {"description": "No offer for this device."},
    status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limited; the poll is skipped."},
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "Transient backend failure; the device retries with backoff."
    },
}


def _service(
    session: Session,
    storage: ObjectStorage,
    settings: Settings,
    verifier: ManifestVerifier | None,
    limiter: InMemoryRateLimiter,
) -> OtaService:
    firmware_service = build_firmware_service(session, storage, settings, verifier)
    return OtaService(
        session,
        storage,
        firmware_service,
        build_update_decision_service(session, firmware_service),
        limiter,
    )


def _write(result: OtaResponse) -> Response:
    headers = dict(_BASE_HEADERS)
    if result.reason and result.status_code in _REASON_HEADER_STATUSES:
        headers["X-OTA-Reason"] = result.reason
    if result.stream is not None:
        if result.content_length is None:
            result.stream.close()
            raise RuntimeError("streamed OTA response requires a content length")
        headers["Content-Length"] = str(result.content_length)
        return StreamingResponse(
            _stream_chunks(result),
            status_code=result.status_code,
            media_type=result.content_type,
            headers=headers,
        )
    return Response(
        content=result.body,
        status_code=result.status_code,
        media_type=result.content_type,
        headers=headers,
    )


def _stream_chunks(result: OtaResponse) -> Iterator[bytes]:
    stream = result.stream
    expected_length = result.content_length
    if stream is None or expected_length is None:
        raise RuntimeError("streamed OTA response is missing stream metadata")

    emitted = 0
    completed = False
    try:
        for chunk in stream.iter_chunks(64 * 1024):
            if not chunk:
                continue
            if emitted + len(chunk) > expected_length:
                logger.error(
                    "firmware_stream_exceeded_expected_size",
                    extra={
                        "extra_fields": {
                            "expected": expected_length,
                            "emitted": emitted + len(chunk),
                        }
                    },
                )
                raise OSError("firmware object exceeded its published size")
            emitted += len(chunk)
            yield chunk
        if emitted != expected_length:
            logger.error(
                "firmware_stream_size_mismatch",
                extra={"extra_fields": {"expected": expected_length, "actual": emitted}},
            )
            return
        completed = True
    except Exception:
        logger.exception("firmware_stream_failed")
        raise
    finally:
        try:
            stream.close()
        except Exception:
            logger.exception("firmware_stream_close_failed")

    if completed and result.on_stream_complete is not None:
        result.on_stream_complete()


@router.get(
    "/api/v1/firmware/{device_type}/{platform}/manifest.json",
    summary="Signed OTA manifest for the authenticated device",
    response_class=Response,
    responses=_DEVICE_AUTH_RESPONSES,
)
def get_manifest(
    device_type: str,
    platform: str,
    x_device_serial: SerialHeader = None,
    x_device_mac: MacHeader = None,
    x_device_token: TokenHeader = None,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
    limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
) -> Response:
    """Return the stored signed manifest, or a documented no-offer response.

    `{device_type}` and `{platform}` must match the authenticated device's own
    device type; this endpoint is provisioned on the device as `OTA_ONLINE_URL`.
    """
    result = _service(session, storage, settings, verifier, limiter).manifest(
        device_type=device_type,
        platform=platform,
        serial=x_device_serial,
        mac=x_device_mac,
        token=x_device_token,
    )
    return _write(result)


@router.get(
    "/firmware/{device_type}/{platform}/v{version}/{filename}",
    summary="Firmware binary for the authenticated device",
    response_class=Response,
    responses=_DEVICE_AUTH_RESPONSES,
)
def get_firmware(
    device_type: str,
    platform: str,
    version: str,
    filename: str,
    x_device_serial: SerialHeader = None,
    x_device_mac: MacHeader = None,
    x_device_token: TokenHeader = None,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
    limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
) -> Response:
    """Stream one published release of the device's own device type.

    The URL comes from the signed manifest, so the `v` prefix and the filename
    must match it exactly. Entitlement is re-decided on every download: serving a
    manifest earlier is not authorization for the binary, and a device whose path
    identity does not match its own device type receives no bytes.
    """
    result = _service(session, storage, settings, verifier, limiter).firmware(
        device_type=device_type,
        platform=platform,
        version=version,
        filename=filename,
        serial=x_device_serial,
        mac=x_device_mac,
        token=x_device_token,
    )
    return _write(result)
