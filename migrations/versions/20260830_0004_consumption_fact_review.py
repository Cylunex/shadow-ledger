"""Add consumption import review, merchant rules, and Archive evidence links.

Revision ID: 20260830_0004
Revises: 20260825_0003
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0004"
down_revision: str | None = "20260825_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(), nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('open','completed')", name="ck_import_batch_state"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_import_batch_idempotency"),
    )
    op.create_index(
        "idx_import_batches_owner_created", "import_batches", ["owner_id", "created_at"]
    )
    op.create_table(
        "import_review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("source_external_id", sa.Text(), nullable=False),
        sa.Column("raw_merchant_name", sa.Text(), nullable=True),
        sa.Column("raw_item_names", sa.JSON(), nullable=False),
        sa.Column("normalized_merchant_id", sa.Uuid(), nullable=True),
        sa.Column("duplicate_of_record_id", sa.Uuid(), nullable=True),
        sa.Column("refund_candidate_entry_id", sa.Uuid(), nullable=True),
        sa.Column("amount_anomaly_reason", sa.String(length=120), nullable=True),
        sa.Column("review_state", sa.String(length=20), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "review_state IN ('pending','resolved','dismissed')",
            name="ck_import_review_state",
        ),
        sa.CheckConstraint("revision > 0", name="ck_import_review_revision"),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["capture_sources.id"]),
        sa.ForeignKeyConstraint(["record_id"], ["ledger_records.id"]),
        sa.ForeignKeyConstraint(["normalized_merchant_id"], ["merchants.id"]),
        sa.ForeignKeyConstraint(["duplicate_of_record_id"], ["ledger_records.id"]),
        sa.ForeignKeyConstraint(["refund_candidate_entry_id"], ["money_entries.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_import_review_owner_state",
        "import_review_items",
        ["owner_id", "review_state", "created_at"],
    )
    op.create_table(
        "merchant_normalization_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("match_kind", sa.String(length=40), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("source_review_item_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("match_kind = 'raw_merchant_exact'", name="ck_merchant_rule_kind"),
        sa.CheckConstraint("revision > 0", name="ck_merchant_rule_revision"),
        sa.CheckConstraint("evidence_count > 0", name="ck_merchant_rule_evidence"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.ForeignKeyConstraint(["source_review_item_id"], ["import_review_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_merchant_rules_lookup",
        "merchant_normalization_rules",
        ["owner_id", "normalized_value", "active"],
    )
    op.create_index(
        "uq_merchant_rules_active",
        "merchant_normalization_rules",
        ["owner_id", "normalized_value"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )
    op.create_table(
        "archive_evidence_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("asset_binding_id", sa.Uuid(), nullable=False),
        sa.Column("archive_uri", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_archive_evidence_revision"),
        sa.ForeignKeyConstraint(["record_id"], ["ledger_records.id"]),
        sa.ForeignKeyConstraint(["asset_binding_id"], ["asset_bindings.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id",
            "record_id",
            "asset_binding_id",
            "archive_uri",
            name="uq_archive_evidence_link",
        ),
    )
    op.create_index(
        "idx_archive_evidence_record",
        "archive_evidence_links",
        ["owner_id", "record_id", "active"],
    )


def downgrade() -> None:
    op.drop_index("idx_archive_evidence_record", table_name="archive_evidence_links")
    op.drop_table("archive_evidence_links")
    op.drop_index("uq_merchant_rules_active", table_name="merchant_normalization_rules")
    op.drop_index("idx_merchant_rules_lookup", table_name="merchant_normalization_rules")
    op.drop_table("merchant_normalization_rules")
    op.drop_index("idx_import_review_owner_state", table_name="import_review_items")
    op.drop_table("import_review_items")
    op.drop_index("idx_import_batches_owner_created", table_name="import_batches")
    op.drop_table("import_batches")
