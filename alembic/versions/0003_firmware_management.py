# OTA-Service — Firmware Management
"""Create firmware_releases, firmware_artifacts and firmware_manifests tables."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_firmware_management"
down_revision: str | Sequence[str] | None = "0002_device_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RELEASE_STATUS = ("uploaded", "validated", "draft", "published", "deprecated", "archived")

NOW = sa.text("now()")


def upgrade() -> None:
    status = sa.Enum(*RELEASE_STATUS, name="firmware_release_status")

    op.create_table(
        "firmware_releases",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "device_type_id",
            sa.BigInteger,
            sa.ForeignKey("device_types.id"),
            nullable=False,
        ),
        sa.Column("version", sa.String(15), nullable=False),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("release_notes", sa.Text, nullable=True),
        sa.Column("status", status, nullable=False, server_default="uploaded"),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("device_type_id", "version"),
    )
    op.create_index("ix_firmware_releases_device_type_id", "firmware_releases", ["device_type_id"])

    op.create_table(
        "firmware_artifacts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "release_id",
            sa.BigInteger,
            sa.ForeignKey("firmware_releases.id"),
            nullable=False,
        ),
        sa.Column("storage_bucket", sa.String(63), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("filename", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("md5", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("release_id"),
    )

    op.create_table(
        "firmware_manifests",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "release_id",
            sa.BigInteger,
            sa.ForeignKey("firmware_releases.id"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.BigInteger,
            sa.ForeignKey("firmware_artifacts.id"),
            nullable=False,
        ),
        sa.Column("version", sa.String(15), nullable=False),
        sa.Column("url", sa.String(128), nullable=False),
        sa.Column("md5", sa.String(32), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("signature", sa.Text, nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("signature_source", sa.String(16), nullable=False),
        sa.Column("signature_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("release_id"),
    )


def downgrade() -> None:
    op.drop_table("firmware_manifests")
    op.drop_table("firmware_artifacts")
    op.drop_index("ix_firmware_releases_device_type_id", table_name="firmware_releases")
    op.drop_table("firmware_releases")
    sa.Enum(name="firmware_release_status").drop(op.get_bind(), checkfirst=True)
