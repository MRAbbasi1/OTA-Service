"""Device-facing URL and object key composition.

This module is the only place that builds an OTA URL or a firmware object key,
so the manifest endpoint, the publication gate, the dashboard, and the tests all
agree by construction. Every segment is validated here before it can reach a URL
or a storage key, which is also what makes path traversal impossible.

Normative model and worked examples: ``docs/16-update-path-and-publication.md``.
Device parser bounds (``docs/OtaManager.md`` §5): manifest ``version`` under 16
characters, manifest ``url`` under 96 characters, MD5 exactly 32 characters,
``size`` from 1 byte through 4 MiB.
"""

from __future__ import annotations

import re

from app.domain.errors import DomainError
from app.domain.version import is_valid_version

CODE_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

CODE_MIN_LENGTH = 3
CODE_MAX_LENGTH = 24
PLATFORM_MIN_LENGTH = 2
PLATFORM_MAX_LENGTH = 16

MANIFEST_PATH_TEMPLATE = "/api/v1/firmware/{code}/{platform}/manifest.json"
FIRMWARE_PATH_TEMPLATE = "/firmware/{code}/{platform}/v{version}/{filename}"
MANIFEST_FILENAME = "manifest.json"

# The device rejects a manifest whose ``url`` field is 96 characters or longer.
MAX_FIRMWARE_URL_LENGTH = 96

# With the canonical delivery host (``cdn.ota-service.example``) and the canonical
# filename the fixed URL overhead is 55 characters, and the variable part is
# code + platform + version. Device-type creation reserves 11 characters for a
# future 999.999.999 version so a type can never be created that is impossible
# to publish safely.
DEVICE_TYPE_PATH_BUDGET = 29
FIRMWARE_URL_BUDGET = 40


def validate_code(code: str) -> str:
    if len(code) < CODE_MIN_LENGTH or len(code) > CODE_MAX_LENGTH:
        raise DomainError("invalid_code")
    if not CODE_PATTERN.match(code):
        raise DomainError("invalid_code")
    return code


def validate_platform(platform: str) -> str:
    if len(platform) < PLATFORM_MIN_LENGTH or len(platform) > PLATFORM_MAX_LENGTH:
        raise DomainError("invalid_platform")
    if not CODE_PATTERN.match(platform):
        raise DomainError("invalid_platform")
    return platform


def validate_version(version: str) -> str:
    if not is_valid_version(version):
        raise DomainError("invalid_version")
    return version


def validate_filename(filename: str) -> str:
    """Reject anything that could escape the derived object prefix.

    The pattern forbids ``/``, ``\\``, a leading dot, whitespace, and
    percent-encoded characters, so ``..`` and absolute paths cannot be formed.
    """
    if filename in {"", ".", ".."} or not FILENAME_PATTERN.match(filename):
        raise DomainError("invalid_filename")
    return filename


def validate_device_type_path(code: str, platform: str) -> None:
    """Validate the two path-bearing device type fields.

    Called when a device type is created, and again before any URL is composed
    so that a legacy row can never produce an unusable URL.
    """
    validate_code(code)
    validate_platform(platform)
    if len(code) + len(platform) > DEVICE_TYPE_PATH_BUDGET:
        raise DomainError("device_type_path_budget_exceeded")


def manifest_path(code: str, platform: str) -> str:
    validate_device_type_path(code, platform)
    return MANIFEST_PATH_TEMPLATE.format(code=code, platform=platform)


def firmware_path(code: str, platform: str, version: str, filename: str) -> str:
    """Return the firmware URL path (no host), with the ``v`` version prefix.

    The ``v`` prefix exists only here and in the object key. The manifest
    ``version`` field and the signed payload carry the bare numeric triple.
    """
    validate_device_type_path(code, platform)
    validate_version(version)
    validate_filename(filename)
    if len(code) + len(platform) + len(version) > FIRMWARE_URL_BUDGET:
        # Canonical-host form of the 96-character bound; the absolute check in
        # `firmware_url` then covers a deployment-specific host.
        raise DomainError("manifest_url_too_long")
    return FIRMWARE_PATH_TEMPLATE.format(
        code=code, platform=platform, version=version, filename=filename
    )


def firmware_url_template(base_url: str) -> str:
    """Descriptive template for operators; not a servable URL on its own."""
    return _join(base_url, FIRMWARE_PATH_TEMPLATE)


def manifest_url(base_url: str, code: str, platform: str) -> str:
    """Absolute manifest URL as provisioned on a device (``OTA_ONLINE_URL``)."""
    return _join(base_url, manifest_path(code, platform))


def firmware_url(
    base_url: str,
    code: str,
    platform: str,
    version: str,
    filename: str,
) -> str:
    """Absolute firmware URL as signed into the manifest ``url`` field.

    Enforced here, against the configured delivery host: the device rejects a
    manifest whose ``url`` is 96 characters or longer.
    """
    url = _join(base_url, firmware_path(code, platform, version, filename))
    if len(url) >= MAX_FIRMWARE_URL_LENGTH:
        raise DomainError("manifest_url_too_long")
    return url


def release_storage_prefix(code: str, platform: str, version: str) -> str:
    """Object key prefix shared by a release's binary and its manifest."""
    validate_device_type_path(code, platform)
    validate_version(version)
    return f"firmware/{code}/{platform}/v{version}"


def storage_key(code: str, platform: str, version: str, filename: str) -> str:
    """Object key for the firmware binary; mirrors the URL path exactly."""
    validate_filename(filename)
    firmware_path(code, platform, version, filename)  # enforces the URL budget too
    return f"{release_storage_prefix(code, platform, version)}/{filename}"


def manifest_storage_key(code: str, platform: str, version: str) -> str:
    """Object key for the signed manifest, next to the binary it belongs to."""
    return f"{release_storage_prefix(code, platform, version)}/{MANIFEST_FILENAME}"


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"
