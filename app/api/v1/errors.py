"""Mapping from domain error codes to HTTP responses.

Domain codes are part of the API contract, so they are mapped once, here, rather
than per router.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException, status

from app.domain.errors import DomainError

UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT

ERROR_STATUS: dict[str, int] = {
    # Device management
    "device_type_exists": status.HTTP_409_CONFLICT,
    "device_type_not_found": status.HTTP_404_NOT_FOUND,
    "device_type_inactive": status.HTTP_409_CONFLICT,
    "device_type_path_budget_exceeded": UNPROCESSABLE,
    "invalid_code": UNPROCESSABLE,
    "invalid_platform": UNPROCESSABLE,
    "serial_exists": status.HTTP_409_CONFLICT,
    "mac_exists": status.HTTP_409_CONFLICT,
    "invalid_mac": UNPROCESSABLE,
    "device_not_found": status.HTTP_404_NOT_FOUND,
    "token_not_found": status.HTTP_404_NOT_FOUND,
    # Firmware
    "release_not_found": status.HTTP_404_NOT_FOUND,
    "version_exists": status.HTTP_409_CONFLICT,
    "invalid_version": UNPROCESSABLE,
    "invalid_filename": UNPROCESSABLE,
    "artifact_empty": UNPROCESSABLE,
    "artifact_too_large": UNPROCESSABLE,
    "artifact_missing": status.HTTP_409_CONFLICT,
    "artifact_mismatch": status.HTTP_409_CONFLICT,
    "manifest_invalid": UNPROCESSABLE,
    "manifest_required": UNPROCESSABLE,
    "missing_signature": UNPROCESSABLE,
    "invalid_signature": UNPROCESSABLE,
    "manifest_version_mismatch": UNPROCESSABLE,
    "manifest_md5_mismatch": UNPROCESSABLE,
    "manifest_size_mismatch": UNPROCESSABLE,
    "manifest_url_mismatch": UNPROCESSABLE,
    "manifest_url_too_long": UNPROCESSABLE,
    "invalid_release_transition": status.HTTP_409_CONFLICT,
    # Update policy
    "policy_not_found": status.HTTP_404_NOT_FOUND,
    "policy_conflict": status.HTTP_409_CONFLICT,
    "policy_immutable": status.HTTP_409_CONFLICT,
    "policy_scope_target_required": UNPROCESSABLE,
    "policy_scope_target_forbidden": UNPROCESSABLE,
    "policy_target_required": UNPROCESSABLE,
    "policy_version_forbidden": UNPROCESSABLE,
    "policy_range_invalid": UNPROCESSABLE,
    "policy_window_invalid": UNPROCESSABLE,
    # This deployment verifies manifest signatures, and this manifest was stored
    # before that was configured: re-upload rather than publish.
    "signature_unverified": status.HTTP_409_CONFLICT,
    "storage_verification_failed": status.HTTP_503_SERVICE_UNAVAILABLE,
    # Administrative access
    "admin_email_exists": status.HTTP_409_CONFLICT,
    "last_super_admin": status.HTTP_409_CONFLICT,
    "invalid_email": UNPROCESSABLE,
    "weak_password": UNPROCESSABLE,
    # The caller is already authenticated here, so naming the failed check is
    # useful rather than a disclosure: it is their own account.
    "admin_not_found": status.HTTP_404_NOT_FOUND,
    "invalid_password": status.HTTP_403_FORBIDDEN,
}


def raise_domain_error(exc: DomainError) -> NoReturn:
    raise HTTPException(
        status_code=ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code},
    )
