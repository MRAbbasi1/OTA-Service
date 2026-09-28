from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _enum(enum_type: type[enum.Enum], name: str) -> Enum:
    """Native PostgreSQL enum storing the member values, not their names."""
    return Enum(
        enum_type,
        name=name,
        native_enum=True,
        values_callable=lambda members: [member.value for member in members],
    )


class DeviceStatus(enum.StrEnum):
    PROVISIONED = "provisioned"
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class FirmwareReleaseStatus(enum.StrEnum):
    """Documented release lifecycle; only ``PUBLISHED`` is deliverable."""

    UPLOADED = "uploaded"
    VALIDATED = "validated"
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class KnownFirmwareSource(enum.StrEnum):
    """Where a known firmware version came from.

    The current OTA request carries no running version, so a version can only be
    administrator-asserted today; ``DEVICE_REPORTED`` is reserved for a future
    authenticated telemetry contract.
    """

    ADMIN_ASSERTED = "admin_asserted"
    DEVICE_REPORTED = "device_reported"


class UpdateAttemptStatus(enum.StrEnum):
    """Server-observed OTA events.

    A subset of the documented list on purpose: a server cannot observe an
    installation, a download the client aborted, or a check that produced nothing
    worth a row (the device's ``last_manifest_check_at`` already records liveness
    without one row per poll).
    """

    UPDATE_OFFERED = "update_offered"
    DOWNLOAD_SERVED = "download_served"


class PolicyScope(enum.StrEnum):
    """How wide a policy reaches. The narrower scope wins."""

    GLOBAL = "global"
    DEVICE_TYPE = "device_type"
    DEVICE = "device"


class PolicyType(enum.StrEnum):
    """What a policy does to eligibility.

    There is deliberately no `allow_downgrade`: the firmware installs only a
    strictly newer version, so such a flag could never do anything except
    mislead an operator (`docs/07-update-policy.md` §7).
    """

    PIN = "pin"
    RANGE = "range"
    DISABLE = "disable"


class AdminRole(enum.StrEnum):
    """Administrative roles (`docs/08-security.md` §7); see `app.domain.rbac`."""

    SUPER_ADMIN = "super_admin"
    FLEET_MANAGER = "fleet_manager"
    FIRMWARE_MANAGER = "firmware_manager"
    VIEWER = "viewer"


class DeviceType(Base):
    __tablename__ = "device_types"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(64))
    hardware_revision: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    devices: Mapped[list[Device]] = relationship(back_populates="device_type")
    releases: Mapped[list[FirmwareRelease]] = relationship(back_populates="device_type")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_type_id: Mapped[int] = mapped_column(ForeignKey("device_types.id"), index=True)
    serial_number: Mapped[int] = mapped_column(BigInteger, unique=True)
    raw_efuse_mac: Mapped[str] = mapped_column(String(17), unique=True)
    network_mac: Mapped[str | None] = mapped_column(String(17))
    status: Mapped[DeviceStatus] = mapped_column(
        _enum(DeviceStatus, "device_status"),
        index=True,
        default=DeviceStatus.PROVISIONED,
        server_default="provisioned",
    )
    ota_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # The known firmware version is always accompanied by its provenance. A served
    # download never updates these; it updates `last_download_served_*` below.
    current_firmware_version: Mapped[str | None] = mapped_column(String(15))
    known_firmware_source: Mapped[str | None] = mapped_column(String(16))
    known_firmware_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_download_served_version: Mapped[str | None] = mapped_column(String(15))
    last_download_served_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_manifest_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_update_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_update_completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    device_type: Mapped[DeviceType] = relationship(back_populates="devices")
    tokens: Mapped[list[DeviceToken]] = relationship(back_populates="device")
    update_attempts: Mapped[list[UpdateAttempt]] = relationship(back_populates="device")


# The same two invariants the migration installs, declared here so a database
# created from metadata (a local bootstrap, a test that skips Alembic) has the
# same integrity guarantees as a migrated one.
POLICY_SCOPE_TARGET_CHECK = (
    "(scope = 'global' AND device_type_id IS NULL AND device_id IS NULL)"
    " OR (scope = 'device_type' AND device_type_id IS NOT NULL AND device_id IS NULL)"
    " OR (scope = 'device' AND device_id IS NOT NULL AND device_type_id IS NULL)"
)
POLICY_TYPE_FIELDS_CHECK = (
    "(policy_type = 'disable' AND target_version IS NULL"
    " AND min_version IS NULL AND max_version IS NULL)"
    " OR (policy_type = 'pin' AND target_version IS NOT NULL"
    " AND min_version IS NULL AND max_version IS NULL)"
    " OR (policy_type = 'range' AND target_version IS NULL"
    " AND (min_version IS NOT NULL OR max_version IS NOT NULL))"
)


class AdminUser(Base):
    """An administrative account.

    `session_version` is bumped on password change and deactivation, which
    invalidates every issued token immediately instead of waiting for the short
    access-token lifetime to elapse (`docs/08-security.md` §5).
    """

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[AdminRole] = mapped_column(_enum(AdminRole, "admin_role"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class UpdatePolicy(Base):
    """An update restriction, at global, device type, or single device scope.

    Policies never select a release set across device types: the decision service
    always draws candidates from the authenticated device's own device type, so a
    policy can only ever narrow what that type already offers.

    `priority` orders policies within one scope; a tie at the winning priority is
    rejected as ambiguous when the policy is written, rather than resolved by
    insertion order (`docs/07-update-policy.md` §§13, 15).
    """

    __tablename__ = "update_policies"
    __table_args__ = (
        CheckConstraint(POLICY_SCOPE_TARGET_CHECK, name="scope_target"),
        CheckConstraint(POLICY_TYPE_FIELDS_CHECK, name="type_fields"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[PolicyScope] = mapped_column(_enum(PolicyScope, "policy_scope"))
    device_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("device_types.id"), index=True, nullable=True
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id"), index=True, nullable=True
    )
    policy_type: Mapped[PolicyType] = mapped_column(_enum(PolicyType, "policy_type"))
    target_version: Mapped[str | None] = mapped_column(String(15))
    min_version: Mapped[str | None] = mapped_column(String(15))
    max_version: Mapped[str | None] = mapped_column(String(15))
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, index=True, default=True, server_default="true"
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    device: Mapped[Device] = relationship(back_populates="tokens")


class FirmwareRelease(Base):
    """A logical firmware version for exactly one device type.

    The release does not carry its own platform: ``code`` and ``platform`` come
    from the parent device type, so the URL path has a single source of truth.
    Once published, the version, artifact, and manifest are immutable; a changed
    binary requires a new version, never an edit.
    """

    __tablename__ = "firmware_releases"
    __table_args__ = (UniqueConstraint("device_type_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_type_id: Mapped[int] = mapped_column(ForeignKey("device_types.id"), index=True)
    version: Mapped[str] = mapped_column(String(15))
    name: Mapped[str | None] = mapped_column(String(128))
    release_notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[FirmwareReleaseStatus] = mapped_column(
        _enum(FirmwareReleaseStatus, "firmware_release_status"),
        default=FirmwareReleaseStatus.UPLOADED,
        server_default="uploaded",
    )
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    device_type: Mapped[DeviceType] = relationship(back_populates="releases")
    artifact: Mapped[FirmwareArtifact | None] = relationship(
        back_populates="release", uselist=False, cascade="all, delete-orphan"
    )
    manifest: Mapped[FirmwareManifest | None] = relationship(
        back_populates="release", uselist=False, cascade="all, delete-orphan"
    )


class FirmwareArtifact(Base):
    """The binary in MinIO. Never stored in PostgreSQL."""

    __tablename__ = "firmware_artifacts"
    __table_args__ = (UniqueConstraint("release_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("firmware_releases.id"))
    storage_bucket: Mapped[str] = mapped_column(String(63))
    storage_key: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    md5: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    release: Mapped[FirmwareRelease] = relationship(back_populates="artifact")


class FirmwareManifest(Base):
    """The signed manifest that is actually served, and its validated field set.

    The stored ``storage_key`` object holds the exact published bytes; the
    manifest endpoint serves them verbatim, so what is audited is literally what
    a device received. ``signature_verified`` records whether the signature was
    cryptographically checked with the production public key; a release whose
    manifest was never verified must not be published.
    """

    __tablename__ = "firmware_manifests"
    __table_args__ = (UniqueConstraint("release_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("firmware_releases.id"))
    artifact_id: Mapped[int] = mapped_column(ForeignKey("firmware_artifacts.id"))
    version: Mapped[str] = mapped_column(String(15))
    url: Mapped[str] = mapped_column(String(128))
    md5: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(BigInteger)
    signature: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(String(512))
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    signature_source: Mapped[str] = mapped_column(String(16))
    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    release: Mapped[FirmwareRelease] = relationship(back_populates="manifest")


class UpdateAttempt(Base):
    """A server-observed OTA event for one device."""

    __tablename__ = "update_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), index=True)
    release_id: Mapped[int | None] = mapped_column(
        ForeignKey("firmware_releases.id"), nullable=True
    )
    from_version: Mapped[str | None] = mapped_column(String(15))
    from_version_source: Mapped[str | None] = mapped_column(String(16))
    to_version: Mapped[str | None] = mapped_column(String(15))
    status: Mapped[UpdateAttemptStatus] = mapped_column(
        _enum(UpdateAttemptStatus, "update_attempt_status")
    )
    bytes_served: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")

    device: Mapped[Device] = relationship(back_populates="update_attempts")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()")
