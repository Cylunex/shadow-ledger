"""Add user use cycles and replayable deterministic forecasts.

Revision ID: 20260831_0005
Revises: 20260830_0004
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0005"
down_revision: str | None = "20260830_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "use_cycles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("item_identity_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('active','completed','cancelled')", name="ck_use_cycle_state"
        ),
        sa.CheckConstraint("revision > 0", name="ck_use_cycle_revision"),
        sa.CheckConstraint(
            "expected_end_at IS NULL OR expected_end_at >= started_at",
            name="ck_use_cycle_expected_end",
        ),
        sa.CheckConstraint(
            "(state='active' AND ended_at IS NULL) OR "
            "(state IN ('completed','cancelled') AND ended_at IS NOT NULL AND ended_at >= started_at)",
            name="ck_use_cycle_state_time",
        ),
        sa.ForeignKeyConstraint(["item_identity_id"], ["item_identities.id"]),
        sa.ForeignKeyConstraint(["source_record_id"], ["ledger_records.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_use_cycles_owner_state", "use_cycles", ["owner_id", "state", "started_at"])
    op.create_index(
        "idx_use_cycles_item", "use_cycles", ["owner_id", "item_identity_id", "started_at"]
    )
    op.create_table(
        "forecast_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=40), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.LargeBinary(), nullable=False),
        sa.Column("output_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("horizon_days BETWEEN 1 AND 365", name="ck_forecast_horizon"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "algorithm_version", "input_hash", name="uq_forecast_run_input"
        ),
    )
    op.create_index("idx_forecast_runs_owner_created", "forecast_runs", ["owner_id", "created_at"])
    op.create_table(
        "forecast_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("target_uri", sa.Text(), nullable=False),
        sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_amount", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('commitment_due','repeat_purchase','use_cycle_end')",
            name="ck_forecast_item_kind",
        ),
        sa.CheckConstraint("state IN ('active','dismissed')", name="ck_forecast_item_state"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_forecast_confidence"),
        sa.CheckConstraint("revision > 0", name="ck_forecast_item_revision"),
        sa.CheckConstraint(
            "expected_amount IS NULL OR (expected_amount > 0 AND currency IS NOT NULL)",
            name="ck_forecast_item_amount",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["forecast_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "source_key", name="uq_forecast_item_source"),
    )
    op.create_index("idx_forecast_items_run_time", "forecast_items", ["run_id", "predicted_at"])


def downgrade() -> None:
    op.drop_index("idx_forecast_items_run_time", table_name="forecast_items")
    op.drop_table("forecast_items")
    op.drop_index("idx_forecast_runs_owner_created", table_name="forecast_runs")
    op.drop_table("forecast_runs")
    op.drop_index("idx_use_cycles_item", table_name="use_cycles")
    op.drop_index("idx_use_cycles_owner_state", table_name="use_cycles")
    op.drop_table("use_cycles")
