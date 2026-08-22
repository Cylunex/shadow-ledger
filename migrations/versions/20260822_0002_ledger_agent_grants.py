"""Add resource-scoped Ledger Agent grants.

Revision ID: 20260822_0002
Revises: bd7c25fb5d21
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0002"
down_revision: str | None = "bd7c25fb5d21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ledger_agent_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.Text(), nullable=False),
        sa.Column("allow_summary", sa.Boolean(), nullable=False),
        sa.Column("allow_records", sa.Boolean(), nullable=False),
        sa.Column("allow_budgets", sa.Boolean(), nullable=False),
        sa.Column("allow_drafts", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_ledger_agent_grant_agent"),
    )
    op.create_index(
        "idx_ledger_agent_grants_owner",
        "ledger_agent_grants",
        ["owner_id", "active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_ledger_agent_grants_owner", table_name="ledger_agent_grants")
    op.drop_table("ledger_agent_grants")
