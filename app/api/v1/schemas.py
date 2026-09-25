from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import PASSWORD_MIN_LENGTH
from app.db.models import (
    AdminRole,
    DeviceStatus,
    FirmwareReleaseStatus,
    PolicyScope,
    PolicyType,
    UpdateAttemptStatus,
)
from app.services.update_decision import UpdateReason

_PATH_SEGMENT = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class DeviceTypeCreate(BaseModel):
    """`code` and `platform` are path segments of every OTA URL for this type."""

    code: str = Field(min_length=3, max_length=24, pattern=_PATH_SEGMENT)
    name: str = Field(min_length=2, max_length=128)
    platform: str = Field(min_length=2, max_length=16, pattern=_PATH_SEGMENT)
    hardware_revision: str | None = Field(default=None, max_length=32)
    description: str | None = None


class DeviceTypeRead(BaseModel):
    """Includes the derived URLs so operators never construct them by hand."""

    id: int
    code: str
    name: str
    platform: str
    hardware_revision: str | None
    description: str | None
    is_active: bool
    created_at: datetime
    manifest_url: str
    firmware_url_template: str


class DeviceCreate(BaseModel):
    device_type_id: int
    serial_number: int = Field(gt=0)
    raw_efuse_mac: str = Field(min_length=12, max_length=17)
    network_mac: str | None = Field(default=None, min_length=12, max_length=17)
    ota_enabled: bool = True


class DeviceRead(BaseModel):
    """Device view.

    `current_firmware_version` is a *known* version with a provenance, never an
    observed fact: the OTA request carries no running version, so today it can
    only be administrator-asserted, and `last_download_served_*` records what
    this server handed out rather than what the device is running.
    """

    id: int
    device_type_id: int
    serial_number: int
    raw_efuse_mac: str
    network_mac: str | None
    status: DeviceStatus
    ota_enabled: bool
    current_firmware_version: str | None
    known_firmware_source: str | None
    known_firmware_observed_at: datetime | None
    last_download_served_version: str | None
    last_download_served_at: datetime | None
    last_update_started_at: datetime | None
    last_update_completed_at: datetime | None
    registered_at: datetime
    last_seen_at: datetime | None
    last_manifest_check_at: datetime | None
    # The value the field team must write into NVS as OTA_ONLINE_URL.
    expected_manifest_url: str


class DeviceFirmwareVersionUpdate(BaseModel):
    """Administratively asserted running version; `null` clears it."""

    version: str | None = Field(default=None, max_length=15)


class UpdateAttemptRead(BaseModel):
    """A server-observed OTA event; never a claim that a firmware booted."""

    id: int
    release_id: int | None
    from_version: str | None
    from_version_source: str | None
    to_version: str | None
    status: UpdateAttemptStatus
    bytes_served: int | None
    created_at: datetime


class UpdatePolicyCreate(BaseModel):
    """A policy definition.

    Only the shape is validated here; the coherence rules (which scope requires
    which target, which policy type allows which version fields, whether an
    equal-priority policy already covers this target) live in the domain and
    service layer, so there is exactly one place they can be got wrong.
    """

    name: str = Field(min_length=2, max_length=128)
    scope: PolicyScope
    policy_type: PolicyType
    device_type_id: int | None = None
    device_id: int | None = None
    target_version: str | None = Field(default=None, max_length=15)
    min_version: str | None = Field(default=None, max_length=15)
    max_version: str | None = Field(default=None, max_length=15)
    priority: int = Field(default=0, ge=0, le=1000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    description: str | None = None


class UpdatePolicyUpdate(BaseModel):
    """Partial edit; unset fields are left alone.

    Scope, policy type, and scope target are not editable: changing any of them
    is a different policy, and rewriting it in place would re-interpret decisions
    it has already explained. `extra="forbid"` rejects them instead of ignoring
    them, so a request can never be half-applied without the caller knowing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=128)
    target_version: str | None = Field(default=None, max_length=15)
    min_version: str | None = Field(default=None, max_length=15)
    max_version: str | None = Field(default=None, max_length=15)
    priority: int | None = Field(default=None, ge=0, le=1000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    description: str | None = None


class UpdatePolicyRead(BaseModel):
    id: int
    name: str
    description: str | None
    scope: PolicyScope
    policy_type: PolicyType
    device_type_id: int | None
    device_id: int | None
    target_version: str | None
    min_version: str | None
    max_version: str | None
    priority: int
    is_active: bool
    starts_at: datetime | None
    ends_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class PolicyActiveUpdate(BaseModel):
    is_active: bool


class UpdateDecisionRead(BaseModel):
    """The dashboard "why?" view of one device's eligibility.

    It is produced by the same service the device endpoint uses, so it can never
    disagree with what the device was actually told.
    """

    device_id: int | None
    update_available: bool
    reason: UpdateReason
    target_version: str | None
    release_id: int | None
    firmware_url: str | None
    policy_id: int | None
    policy_scope: PolicyScope | None
    policy_name: str | None
    known_version: str | None
    known_version_source: str | None
    explanation: str


class AdminLogin(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AdminUserRead(BaseModel):
    """An administrative account. `capabilities` is what the role actually grants."""

    id: int
    email: str
    role: AdminRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    capabilities: list[str]


class AdminSessionRead(BaseModel):
    """A logged-in session.

    The session cookie is `HttpOnly` and deliberately not here; `csrf_token` is
    returned so a browser client can echo it back in the CSRF header, and it is
    also set as a readable cookie for clients that prefer the cookie form.
    """

    admin: AdminUserRead
    csrf_token: str
    expires_at: datetime
    csrf_header: str


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)
    role: AdminRole


class AdminRoleUpdate(BaseModel):
    role: AdminRole


class AdminActiveUpdate(BaseModel):
    is_active: bool


class AdminPasswordChange(BaseModel):
    """Self-service password change; the current password is required."""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)


class AuditEventRead(BaseModel):
    """An audit record. It never contains a secret, by construction."""

    id: int
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, object] | None
    created_at: datetime


class FirmwareDistributionRow(BaseModel):
    """One bar of the firmware-distribution chart.

    `unknown` is a first-class row rather than an omission: the platform cannot
    observe what it was never told (`docs/15-implementation-decisions.md` §5).
    """

    device_type_code: str
    platform: str
    version: str | None
    version_source: str | None
    device_count: int


class FleetSummaryRead(BaseModel):
    total_devices: int
    active_devices: int
    provisioned_devices: int
    disabled_devices: int
    retired_devices: int
    ota_disabled_devices: int
    total_device_types: int
    published_releases: int
    active_policies: int
    checks_last_24h: int
    offers_last_24h: int
    downloads_last_24h: int
    devices_needing_attention: int
    generated_at: datetime


class UpdateActivityRow(BaseModel):
    id: int
    device_id: int
    device_serial: int
    device_type_code: str
    status: UpdateAttemptStatus
    from_version: str | None
    to_version: str | None
    bytes_served: int | None
    created_at: datetime


class UpdateActivityRead(BaseModel):
    items: list[UpdateActivityRow]
    offers_last_24h: int
    downloads_last_24h: int
    offers_last_7d: int
    downloads_last_7d: int


class AttentionCategoryRead(BaseModel):
    category: str
    device_count: int
    explanation: str


class DeviceHealthRead(BaseModel):
    categories: list[AttentionCategoryRead]
    total_devices: int


class TokenIssued(BaseModel):
    """Returned once at issuance/rotation; the plaintext is never stored."""

    token_id: int
    device_id: int
    token: str


class DeviceStatusUpdate(BaseModel):
    status: DeviceStatus


class OtaEnabledUpdate(BaseModel):
    ota_enabled: bool


class FirmwareArtifactRead(BaseModel):
    filename: str
    content_type: str
    size: int
    md5: str
    sha256: str
    storage_bucket: str
    storage_key: str


class FirmwareManifestRead(BaseModel):
    version: str
    url: str
    md5: str
    size: int
    signature: str
    storage_key: str
    manifest_sha256: str
    signature_source: str
    signature_verified: bool
    validated_at: datetime


class FirmwareReleaseRead(BaseModel):
    """Release view with the derived URL material an operator must verify."""

    id: int
    device_type_id: int
    device_type_code: str
    platform: str
    version: str
    name: str | None
    release_notes: str | None
    status: FirmwareReleaseStatus
    created_by: str
    created_at: datetime
    published_at: datetime | None
    manifest_url: str
    storage_prefix: str


class FirmwareReleaseDetail(FirmwareReleaseRead):
    artifact: FirmwareArtifactRead | None
    manifest: FirmwareManifestRead | None
    firmware_url: str | None
