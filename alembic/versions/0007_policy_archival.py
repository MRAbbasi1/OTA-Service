"""Retain archived update policies for audit and historical explanation."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_policy_archival"
down_revision: str | Sequence[str] | None = "0006_admin_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "update_policies",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_update_policies_archived_at",
        "update_policies",
        ["archived_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_update_policies_archived_at", table_name="update_policies")
    op.drop_column("update_policies", "archived_at")
