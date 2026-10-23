"""Make raw_efuse_mac optional on devices."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_optional_raw_efuse_mac"
down_revision: str | Sequence[str] | None = "0009_device_communications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "devices",
        "raw_efuse_mac",
        existing_type=sa.String(17),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "devices",
        "raw_efuse_mac",
        existing_type=sa.String(17),
        nullable=False,
    )
