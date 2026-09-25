# OTA-Service — Update Policy Engine
"""Add the update_policies table.

Policies are the only place eligibility can be narrowed, so the schema enforces
what must hold for any policy row regardless of how it was written: the scope and
the policy type cannot contradict their own fields. The equal-priority ambiguity
rule is time-based and therefore checked in the service, not here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_update_policy"
down_revision: str | Sequence[str] | None = "0004_ota_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_SCOPE = ("global", "device_type", "device")
POLICY_TYPE = ("pin", "range", "disable")

NOW = sa.text("now()")

# Kept in sync with ``app.db.models`` by hand: a migration is a frozen record of
# what was applied, so it must not import the live model definitions. The names
# are the suffix the metadata naming convention expands.
SCOPE_TARGET_CHECK = (
    "(scope = 'global' AND device_type_id IS NULL AND device_id IS NULL)"
    " OR (scope = 'device_type' AND device_type_id IS NOT NULL AND device_id IS NULL)"
    " OR (scope = 'device' AND device_id IS NOT NULL AND device_type_id IS NULL)"
)

TYPE_FIELDS_CHECK = (
    "(policy_type = 'disable' AND target_version IS NULL"
    " AND min_version IS NULL AND max_version IS NULL)"
    " OR (policy_type = 'pin' AND target_version IS NOT NULL"
    " AND min_version IS NULL AND max_version IS NULL)"
    " OR (policy_type = 'range' AND target_version IS NULL"
    " AND (min_version IS NOT NULL OR max_version IS NOT NULL))"
)


def upgrade() -> None:
    scope = sa.Enum(*POLICY_SCOPE, name="policy_scope")
    policy_type = sa.Enum(*POLICY_TYPE, name="policy_type")
    op.create_table(
        "update_policies",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("scope", scope, nullable=False),
        sa.Column("device_type_id", sa.BigInteger, sa.ForeignKey("device_types.id"), nullable=True),
        sa.Column("device_id", sa.BigInteger, sa.ForeignKey("devices.id"), nullable=True),
        sa.Column("policy_type", policy_type, nullable=False),
        sa.Column("target_version", sa.String(15), nullable=True),
        sa.Column("min_version", sa.String(15), nullable=True),
        sa.Column("max_version", sa.String(15), nullable=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(SCOPE_TARGET_CHECK, name="scope_target"),
        sa.CheckConstraint(TYPE_FIELDS_CHECK, name="type_fields"),
    )
    op.create_index("ix_update_policies_device_id", "update_policies", ["device_id"])
    op.create_index("ix_update_policies_device_type_id", "update_policies", ["device_type_id"])
    op.create_index("ix_update_policies_is_active", "update_policies", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_update_policies_is_active", table_name="update_policies")
    op.drop_index("ix_update_policies_device_type_id", table_name="update_policies")
    op.drop_index("ix_update_policies_device_id", table_name="update_policies")
    op.drop_table("update_policies")
    sa.Enum(name="policy_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="policy_scope").drop(op.get_bind(), checkfirst=True)
