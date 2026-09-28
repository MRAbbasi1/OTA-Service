# OTA-Service — Administrative Access
"""Add the admin_users table.

No row is created here. The first administrator is created only by the audited,
idempotent deployment command, with one-time environment-provided credentials
(`docs/15-implementation-decisions.md` §8), so a migrated database has no default
account and no default password to forget to change.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_admin_users"
down_revision: str | Sequence[str] | None = "0005_update_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_ROLE = ("super_admin", "fleet_manager", "firmware_manager", "viewer")

NOW = sa.text("now()")


def upgrade() -> None:
    role = sa.Enum(*ADMIN_ROLE, name="admin_role")
    op.create_table(
        "admin_users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", role, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("session_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )


def downgrade() -> None:
    op.drop_table("admin_users")
    sa.Enum(name="admin_role").drop(op.get_bind(), checkfirst=True)
