"""Firmware version rules.

The device compares versions as ``major.minor.patch`` integers and its parser
requires the version string to be shorter than 16 characters
(``docs/OtaManager.md`` §5). Pre-release and build suffixes are not supported.
String comparison is never valid for firmware eligibility.
"""

from __future__ import annotations

import re

VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

MAX_VERSION_LENGTH = 15  # the device parser requires < 16 characters


def is_valid_version(version: str) -> bool:
    return bool(VERSION_PATTERN.match(version)) and len(version) <= MAX_VERSION_LENGTH


def parse_version(version: str) -> tuple[int, int, int]:
    """Return the numeric sort key for a version.

    Raises ``ValueError`` for anything the device could not compare.
    """
    if not is_valid_version(version):
        raise ValueError(f"invalid firmware version: {version!r}")
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)
