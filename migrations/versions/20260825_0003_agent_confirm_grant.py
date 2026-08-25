"""Add an explicit resource grant for reviewed Agent record confirmation.

Revision ID: 20260825_0003
Revises: 20260822_0002
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0003"
down_revision: str | None = "20260822_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ledger_agent_grants",
        sa.Column("allow_confirm", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("ledger_agent_grants", "allow_confirm")
