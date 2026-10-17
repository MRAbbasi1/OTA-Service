"""Retain device request/response evidence independently of administrative audit."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_device_communications"
down_revision: str | Sequence[str] | None = "0008_device_token_cascade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_communications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("device_id", sa.BigInteger(), sa.ForeignKey("devices.id")),
        sa.Column("claimed_serial", sa.String(19)),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("bytes_expected", sa.BigInteger()),
        sa.Column("bytes_sent", sa.BigInteger()),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    for column in ("request_id", "device_id", "claimed_serial", "kind", "outcome", "created_at"):
        op.create_index(f"ix_device_communications_{column}", "device_communications", [column])


def downgrade() -> None:
    op.drop_table("device_communications")
