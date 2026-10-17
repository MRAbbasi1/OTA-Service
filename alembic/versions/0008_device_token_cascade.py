"""Delete device-owned token records when an unused device is purged."""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_device_token_cascade"
down_revision: str | Sequence[str] | None = "0007_policy_archival"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_device_tokens_device_id_devices",
        "device_tokens",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_device_tokens_device_id_devices",
        "device_tokens",
        "devices",
        ["device_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_device_tokens_device_id_devices",
        "device_tokens",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_device_tokens_device_id_devices",
        "device_tokens",
        "devices",
        ["device_id"],
        ["id"],
    )
