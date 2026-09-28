"""OTA-Service foundation baseline.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-09-25
"""

from collections.abc import Sequence

revision: str = "0001_foundation"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish the Alembic baseline before domain tables are introduced."""


def downgrade() -> None:
    """The baseline has no schema objects to remove."""
