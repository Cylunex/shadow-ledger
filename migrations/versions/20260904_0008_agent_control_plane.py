"""Durable Agent approval and query evidence; never infer historical approvals."""

import sqlalchemy as sa
from alembic import op

revision = "20260904_0008"
down_revision = "20260904_0007"
branch_labels = None
depends_on = None


def stamp(name="created_at", nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def ident():
    return sa.Column("id", sa.Uuid(), primary_key=True)


def col(name, type_=sa.Text(), nullable=False):
    return sa.Column(name, type_, nullable=nullable)


def upgrade():
    op.create_table(
        "agent_intents",
        ident(),
        col("owner_id"),
        col("agent_id", sa.String(64)),
        col("request_key", sa.String(64)),
        col("record_id", sa.Uuid()),
        col("revision", sa.Integer()),
        col("action", sa.String(16)),
        col("snapshot", sa.JSON()),
        col("args_hash", sa.String(64)),
        col("state", sa.String(24)),
        stamp("expires_at"),
        stamp(),
        stamp("updated_at"),
        sa.UniqueConstraint("owner_id", "agent_id", "request_key", name="uq_agent_intent_request"),
        sa.CheckConstraint("action IN ('confirm','reject')", name="ck_agent_intent_action"),
        sa.CheckConstraint(
            "state IN ('awaiting_human','approved','rejected','executed')",
            name="ck_agent_intent_state",
        ),
    )
    op.create_index("idx_agent_intent_queue", "agent_intents", ["owner_id", "state", "created_at"])
    op.create_table(
        "agent_policy_decisions",
        ident(),
        sa.Column("intent_id", sa.Uuid(), sa.ForeignKey("agent_intents.id"), nullable=False),
        col("verdict", sa.String(12)),
        col("reason_codes", sa.JSON()),
        col("policy_digest", sa.String(64)),
        col("state_hash", sa.String(64)),
        stamp(),
    )
    op.create_table(
        "agent_approval_grants",
        ident(),
        sa.Column("intent_id", sa.Uuid(), sa.ForeignKey("agent_intents.id"), nullable=False),
        sa.Column(
            "decision_id", sa.Uuid(), sa.ForeignKey("agent_policy_decisions.id"), nullable=False
        ),
        col("owner_id"),
        col("approved_by"),
        col("args_hash", sa.String(64)),
        col("policy_digest", sa.String(64)),
        col("capability_hash", sa.String(64)),
        stamp("expires_at"),
        stamp("consumed_at", True),
        stamp(),
        sa.UniqueConstraint("intent_id", name="uq_agent_approval_intent"),
    )
    op.create_table(
        "agent_execution_receipts",
        ident(),
        col("owner_id"),
        col("agent_id", sa.String(64)),
        sa.Column("intent_id", sa.Uuid(), sa.ForeignKey("agent_intents.id"), nullable=False),
        sa.Column("grant_id", sa.Uuid(), sa.ForeignKey("agent_approval_grants.id"), nullable=False),
        col("payload", sa.JSON()),
        col("content_hash", sa.String(64)),
        stamp(),
        sa.UniqueConstraint("grant_id", name="uq_agent_receipt_grant"),
    )
    op.create_table(
        "agent_query_runs",
        ident(),
        col("owner_id"),
        col("agent_id", sa.String(64)),
        col("catalog_hash", sa.String(64)),
        col("query_fingerprint", sa.String(64)),
        col("result", sa.JSON()),
        col("result_hash", sa.String(64)),
        stamp("expires_at"),
        stamp(),
    )
    op.create_index("idx_agent_query_owner", "agent_query_runs", ["owner_id", "created_at"])
    op.create_table(
        "agent_catalog_snapshots",
        sa.Column("catalog_hash", sa.String(64), primary_key=True),
        col("owner_id"),
        col("agent_id", sa.String(64)),
        col("snapshot", sa.JSON()),
        stamp(),
    )


def downgrade():
    for table in (
        "agent_catalog_snapshots",
        "agent_query_runs",
        "agent_execution_receipts",
        "agent_approval_grants",
        "agent_policy_decisions",
        "agent_intents",
    ):
        op.drop_table(table)
