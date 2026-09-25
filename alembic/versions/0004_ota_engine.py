# OTA-Service — OTA Engine
"""Add device firmware-state provenance and the update_attempts table."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_ota_engine"
down_revision: str | Sequence[str] | None = "0003_firmware_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ATTEMPT_STATUS = ("update_offered", "download_served")

NOW = sa.text("now()")


def upgrade() -> None:
    op.add_column("devices", sa.Column("known_firmware_source", sa.String(16), nullable=True))
    op.add_column(
        "devices",
        sa.Column("known_firmware_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices", sa.Column("last_download_served_version", sa.String(15), nullable=True)
    )
    op.add_column(
        "devices",
        sa.Column("last_download_served_at", sa.DateTime(timezone=True), nullable=True),
    )

    status = sa.Enum(*ATTEMPT_STATUS, name="update_attempt_status")
    op.create_table(
        "update_attempts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("device_id", sa.BigInteger, sa.ForeignKey("devices.id"), nullable=False),
        sa.Column(
            "release_id",
            sa.BigInteger,
            sa.ForeignKey("firmware_releases.id"),
            nullable=True,
        ),
        sa.Column("from_version", sa.String(15), nullable=True),
        sa.Column("from_version_source", sa.String(16), nullable=True),
        sa.Column("to_version", sa.String(15), nullable=True),
        sa.Column("status", status, nullable=False),
        sa.Column("bytes_served", sa.BigInteger, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_index("ix_update_attempts_device_id", "update_attempts", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_update_attempts_device_id", table_name="update_attempts")
    op.drop_table("update_attempts")
    sa.Enum(name="update_attempt_status").drop(op.get_bind(), checkfirst=True)
    op.drop_column("devices", "last_download_served_at")
    op.drop_column("devices", "last_download_served_version")
    op.drop_column("devices", "known_firmware_observed_at")
    op.drop_column("devices", "known_firmware_source")
