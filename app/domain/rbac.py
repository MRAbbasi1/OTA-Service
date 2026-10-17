"""Administrative roles and the capabilities they hold.

Authorization is expressed as capabilities, and roles are only bundles of them
(`docs/08-security.md` §7). Endpoints check a capability, never a role name, so
adding a role is a one-line change here and never a hunt through the API layer.

The bundles are deliberately small and non-overlapping where they can be: a
firmware manager cannot change who is enrolled, a fleet manager cannot publish a
release, and neither can create administrators.
"""

from __future__ import annotations

import enum

from app.db.models import AdminRole


class Capability(enum.StrEnum):
    DASHBOARD_READ = "dashboard:read"
    DEVICES_READ = "devices:read"
    DEVICES_WRITE = "devices:write"
    DEVICE_TYPES_CREATE = "device_types:create"
    DEVICE_TYPES_UPDATE = "device_types:update"
    DEVICE_TYPES_DELETE = "device_types:delete"
    DEVICES_CREATE = "devices:create"
    DEVICES_UPDATE_IDENTITY = "devices:update_identity"
    DEVICES_CHANGE_LIFECYCLE = "devices:change_lifecycle"
    DEVICES_PURGE = "devices:purge"
    DEVICE_TOKENS_MANAGE = "device_tokens:manage"
    FIRMWARE_READ = "firmware:read"
    FIRMWARE_WRITE = "firmware:write"
    FIRMWARE_UPLOAD = "firmware:upload"
    FIRMWARE_UPDATE_METADATA = "firmware:update_metadata"
    FIRMWARE_CHANGE_LIFECYCLE = "firmware:change_lifecycle"
    POLICIES_READ = "policies:read"
    POLICIES_WRITE = "policies:write"
    POLICIES_CREATE = "policies:create"
    POLICIES_UPDATE = "policies:update"
    POLICIES_CHANGE_LIFECYCLE = "policies:change_lifecycle"
    AUDIT_READ = "audit:read"
    MONITORING_READ = "monitoring:read"
    LOGS_READ = "logs:read"
    ADMINS_MANAGE = "admins:manage"
    ADMINS_CREATE = "admins:create"
    ADMINS_CHANGE_ROLE = "admins:change_role"
    ADMINS_CHANGE_STATUS = "admins:change_status"


_READ_ONLY = frozenset(
    {
        Capability.DASHBOARD_READ,
        Capability.DEVICES_READ,
        Capability.FIRMWARE_READ,
        Capability.POLICIES_READ,
    }
)

ROLE_CAPABILITIES: dict[AdminRole, frozenset[Capability]] = {
    AdminRole.SUPER_ADMIN: frozenset(Capability),
    AdminRole.FLEET_MANAGER: _READ_ONLY
    | {
        Capability.DEVICES_WRITE,
        Capability.DEVICE_TYPES_CREATE,
        Capability.DEVICE_TYPES_UPDATE,
        Capability.DEVICE_TYPES_DELETE,
        Capability.DEVICES_CREATE,
        Capability.DEVICES_UPDATE_IDENTITY,
        Capability.DEVICES_CHANGE_LIFECYCLE,
        Capability.DEVICE_TOKENS_MANAGE,
        Capability.POLICIES_WRITE,
        Capability.POLICIES_CREATE,
        Capability.POLICIES_UPDATE,
        Capability.POLICIES_CHANGE_LIFECYCLE,
        Capability.AUDIT_READ,
    },
    AdminRole.FIRMWARE_MANAGER: _READ_ONLY
    | {
        Capability.FIRMWARE_WRITE,
        Capability.FIRMWARE_UPLOAD,
        Capability.FIRMWARE_UPDATE_METADATA,
        Capability.FIRMWARE_CHANGE_LIFECYCLE,
        Capability.AUDIT_READ,
    },
    AdminRole.VIEWER: _READ_ONLY,
}


def has_capability(role: AdminRole, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def capabilities_for(role: AdminRole) -> list[str]:
    """The role's capabilities, sorted, for the API to show an operator."""
    return sorted(capability.value for capability in ROLE_CAPABILITIES.get(role, frozenset()))
