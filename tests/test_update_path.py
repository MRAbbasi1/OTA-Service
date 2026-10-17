from __future__ import annotations

import pytest

from app.domain.errors import DomainError
from app.domain.update_path import (
    DEVICE_TYPE_PATH_BUDGET,
    FIRMWARE_URL_BUDGET,
    MAX_FIRMWARE_URL_LENGTH,
    firmware_path,
    firmware_url,
    firmware_url_template,
    manifest_storage_key,
    manifest_url,
    release_storage_prefix,
    storage_key,
    validate_code,
    validate_device_type_path,
    validate_filename,
    validate_platform,
)

CODE = "bcs-controller-v1"
PLATFORM = "esp32-s3"
VERSION = "2.6.1"
FILENAME = "Controller.ino.bin"

MANIFEST_BASE = "https://api.ota-service.example"
FIRMWARE_BASE = "https://cdn.ota-service.example"


def test_documented_manifest_url() -> None:
    assert (
        manifest_url(MANIFEST_BASE, CODE, PLATFORM)
        == "https://api.ota-service.example/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json"
    )


def test_documented_firmware_url_and_v_prefix() -> None:
    url = firmware_url(FIRMWARE_BASE, CODE, PLATFORM, VERSION, FILENAME)
    assert (
        url
        == "https://cdn.ota-service.example/firmware/bcs-controller-v1/esp32-s3/v2.6.1/Controller.ino.bin"
    )
    assert len(url) < MAX_FIRMWARE_URL_LENGTH
    # The v prefix belongs to the path only; the signed payload carries "2.6.1".
    assert "/v2.6.1/" in url
    assert "vv" not in url


def test_base_url_trailing_slash_is_normalised() -> None:
    assert firmware_url(f"{FIRMWARE_BASE}/", CODE, PLATFORM, VERSION, FILENAME) == firmware_url(
        FIRMWARE_BASE, CODE, PLATFORM, VERSION, FILENAME
    )


def test_url_template_is_purely_descriptive() -> None:
    assert firmware_url_template(FIRMWARE_BASE) == (
        "https://cdn.ota-service.example/firmware/{code}/{platform}/v{version}/{filename}"
    )


@pytest.mark.parametrize(
    "code",
    ["ab", "a" * 25, "Bcs-Controller", "bcs_controller", "bcs-", "-bcs", "bcs--controller", ""],
)
def test_invalid_codes_are_rejected(code: str) -> None:
    with pytest.raises(DomainError) as excinfo:
        validate_code(code)
    assert excinfo.value.code == "invalid_code"


@pytest.mark.parametrize("code", ["bcs", "bcs-controller-v1", "a" * 24])
def test_valid_codes_are_accepted(code: str) -> None:
    assert validate_code(code) == code


@pytest.mark.parametrize("platform", ["e", "x" * 17, "ESP32-S3", "esp32_s3", ""])
def test_invalid_platforms_are_rejected(platform: str) -> None:
    with pytest.raises(DomainError) as excinfo:
        validate_platform(platform)
    assert excinfo.value.code == "invalid_platform"


def test_device_type_path_budget_boundary() -> None:
    # 21 + 8 = 29 is the reserved budget for a future 999.999.999 version.
    validate_device_type_path("bcs-controller-v1-abc", PLATFORM)
    with pytest.raises(DomainError) as excinfo:
        validate_device_type_path("bcs-controller-v1-abcd", PLATFORM)
    assert excinfo.value.code == "device_type_path_budget_exceeded"
    assert 21 + len(PLATFORM) == DEVICE_TYPE_PATH_BUDGET


def test_firmware_url_budget_boundary() -> None:
    long_version = "1000.200.300"  # 12 characters
    budget_base = "https://cdn.ota.example"
    accepted = firmware_url(budget_base, "bcs-controller-v1-ab", PLATFORM, long_version, FILENAME)
    assert len("bcs-controller-v1-ab") + len(PLATFORM) + len(long_version) == FIRMWARE_URL_BUDGET
    assert len(accepted) == MAX_FIRMWARE_URL_LENGTH - 1

    with pytest.raises(DomainError) as excinfo:
        firmware_url(budget_base, "bcs-controller-v1-abc", PLATFORM, long_version, FILENAME)
    assert excinfo.value.code == "manifest_url_too_long"


def test_long_delivery_host_is_also_bounded() -> None:
    base = "https://very-long-delivery-hostname.ota-service.example"
    with pytest.raises(DomainError) as excinfo:
        firmware_url(base, CODE, PLATFORM, VERSION, FILENAME)
    assert excinfo.value.code == "manifest_url_too_long"


@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".",
        "..",
        "../evil.bin",
        "..%2fevil.bin",
        "/etc/passwd",
        "sub/dir.bin",
        "back\\slash.bin",
        ".hidden",
        "a" * 33,
        "bad name.bin",
    ],
)
def test_unsafe_filenames_are_rejected(filename: str) -> None:
    with pytest.raises(DomainError) as excinfo:
        validate_filename(filename)
    assert excinfo.value.code == "invalid_filename"


@pytest.mark.parametrize("filename", ["Controller.ino.bin", "a", "firmware_v2.bin", "A1.bin"])
def test_safe_filenames_are_accepted(filename: str) -> None:
    assert validate_filename(filename) == filename


def test_storage_key_mirrors_the_url_path() -> None:
    path = firmware_path(CODE, PLATFORM, VERSION, FILENAME)
    key = storage_key(CODE, PLATFORM, VERSION, FILENAME)
    assert key == path.lstrip("/")
    assert key == "firmware/bcs-controller-v1/esp32-s3/v2.6.1/Controller.ino.bin"
    assert manifest_storage_key(CODE, PLATFORM, VERSION) == (
        "firmware/bcs-controller-v1/esp32-s3/v2.6.1/manifest.json"
    )
    assert release_storage_prefix(CODE, PLATFORM, VERSION) == (
        "firmware/bcs-controller-v1/esp32-s3/v2.6.1"
    )


def test_traversal_cannot_reach_the_key_or_the_url() -> None:
    for bad in ("../../etc/passwd", "..%2f..%2fetc", "/absolute"):
        with pytest.raises(DomainError):
            storage_key(CODE, PLATFORM, VERSION, bad)
        with pytest.raises(DomainError):
            firmware_url(FIRMWARE_BASE, CODE, PLATFORM, VERSION, bad)
        with pytest.raises(DomainError):
            firmware_path(bad, PLATFORM, VERSION, FILENAME)


def test_version_prefix_is_not_accepted_in_the_version_segment() -> None:
    with pytest.raises(DomainError) as excinfo:
        firmware_path(CODE, PLATFORM, "v2.6.1", FILENAME)
    assert excinfo.value.code == "invalid_version"
