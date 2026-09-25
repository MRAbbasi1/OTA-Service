# OTA-Service — Device Management
"""Create device_types, devices, device_tokens and audit_events tables."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_device_management"
down_revision: str | Sequence[str] | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEVICE_STATUS = ("provisioned", "active", "disabled", "retired")


def upgrade() -> None:
    status = sa.Enum(*DEVICE_STATUS, name="device_status")

    op.create_table(
        "device_types",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("hardware_revision", sa.String(32), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "device_type_id",
            sa.BigInteger,
            sa.ForeignKey("device_types.id"),
            nullable=False,
        ),
        sa.Column("serial_number", sa.BigInteger, nullable=False, unique=True),
        sa.Column("raw_efuse_mac", sa.String(17), nullable=False, unique=True),
        sa.Column("network_mac", sa.String(17), nullable=True),
        sa.Column("status", status, nullable=False, server_default="provisioned"),
        sa.Column("ota_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("current_firmware_version", sa.String(15), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_manifest_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_update_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_update_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_devices_device_type_id", "devices", ["device_type_id"])
    op.create_index("ix_devices_status", "devices", ["status"])

    op.create_table(
        "device_tokens",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("device_id", sa.BigInteger, sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_device_tokens_device_id", "device_tokens", ["device_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("detail", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("device_tokens")
    op.drop_table("devices")
    op.drop_table("device_types")
    sa.Enum(name="device_status").drop(op.get_bind(), checkfirst=True)
