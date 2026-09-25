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
    FIRMWARE_READ = "firmware:read"
    FIRMWARE_WRITE = "firmware:write"
    POLICIES_READ = "policies:read"
    POLICIES_WRITE = "policies:write"
    AUDIT_READ = "audit:read"
    ADMINS_MANAGE = "admins:manage"


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
        Capability.POLICIES_WRITE,
        Capability.AUDIT_READ,
    },
    AdminRole.FIRMWARE_MANAGER: _READ_ONLY
    | {
        Capability.FIRMWARE_WRITE,
        Capability.AUDIT_READ,
    },
    AdminRole.VIEWER: _READ_ONLY,
}


def has_capability(role: AdminRole, capability: Capability) -> bool:
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def capabilities_for(role: AdminRole) -> list[str]:
    """The role's capabilities, sorted, for the API to show an operator."""
    return sorted(capability.value for capability in ROLE_CAPABILITIES.get(role, frozenset()))
