"""The audit boundary accepts only reviewed fields, on both write and read."""

from __future__ import annotations

import re
import uuid

SAFE_FIELDS = frozenset(
    {
        "status",
        "ota_enabled",
        "version",
        "role",
        "scope",
        "policy_type",
        "priority",
        "device_id",
        "device_type_id",
        "serial_number",
        "size",
        "md5",
        "sha256",
        "email",
        "is_active",
        "target_version",
        "min_version",
        "max_version",
        "starts_at",
        "ends_at",
    }
)
CHANGE_FIELDS = SAFE_FIELDS | {
    "name",
    "description",
    "hardware_revision",
    "raw_efuse_mac",
    "release_notes",
    "code",
    "platform",
}


def safe_request_id(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value is not None else None
    except (ValueError, AttributeError):
        return None


def safe_detail(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if key == "request_id":
            request_id = safe_request_id(item)
            if request_id is not None:
                result[key] = request_id
        elif key in {"before", "after"} and isinstance(item, dict):
            cleaned = safe_detail({k: v for k, v in item.items() if k in SAFE_FIELDS})
            if cleaned:
                result[key] = cleaned
        elif key == "changed" and isinstance(item, list):
            result[key] = [
                entry for entry in item[:32] if isinstance(entry, str) and entry in CHANGE_FIELDS
            ]
        elif (
            key == "reason"
            and isinstance(item, str)
            and item
            in {
                "invalid_credentials",
                "invalid_password",
                "admin_inactive",
                "admin_not_found",
            }
        ):
            result[key] = item
        elif key in SAFE_FIELDS:
            # These fields are schema-validated identifiers/enums, never arbitrary prose.
            if item is None or isinstance(item, (bool, int)):
                result[key] = item
            elif isinstance(item, str) and len(item) <= 128:
                if key == "email":
                    if re.fullmatch(r"[^\s@]+@[^\s@]+", item):
                        result[key] = item
                elif re.fullmatch(r"[A-Za-z0-9_.:+@-]*", item):
                    result[key] = item
    return result or None
