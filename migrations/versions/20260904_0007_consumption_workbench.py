"""Append-only source observations and cross-run suggestion feedback."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "20260904_0007"
down_revision = "20260903_0006"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "suggestion_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("episode_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("owner_id", "episode_key", name="uq_feedback_episode"),
        sa.CheckConstraint(
            "state IN ('dismissed','snoozed','handled','active')", name="ck_feedback_state"
        ),
        sa.CheckConstraint("revision > 0", name="ck_feedback_revision"),
        sa.CheckConstraint(
            "state <> 'snoozed' OR snoozed_until IS NOT NULL", name="ck_feedback_snooze"
        ),
    )
    op.create_table(
        "source_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("capture_sources.id"), nullable=False),
        sa.Column("external_revision", sa.String(200), nullable=False),
        sa.Column("raw_hash", sa.LargeBinary(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("candidate", sa.JSON()),
        sa.Column("field_evidence", sa.JSON(), nullable=False),
        sa.Column("parser", sa.String(80), nullable=False),
        sa.Column("parser_version", sa.String(40), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("source_id", "external_revision", name="uq_observation_version"),
        sa.CheckConstraint(
            "state IN ('baseline','pending','kept','applied')", name="ck_observation_state"
        ),
        sa.CheckConstraint("revision > 0", name="ck_observation_revision"),
    )
    op.create_index(
        "idx_observation_owner_state",
        "source_observations",
        ["owner_id", "state", "created_at", "id"],
    )
    # Preserve dismissals made before cross-run feedback existed. This algorithm is
    # frozen here, rather than importing changing application code into migrations.
    connection = op.get_bind()
    feedback = sa.table(
        "suggestion_feedback",
        sa.column("id", sa.Uuid()),
        sa.column("owner_id"),
        sa.column("episode_key"),
        sa.column("state"),
        sa.column("revision"),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    seen = set()
    now = datetime.now(UTC)
    for row in connection.execute(
        sa.text("""
        SELECT r.owner_id, i.kind, i.target_uri, i.source_key, i.evidence
        FROM forecast_items i JOIN forecast_runs r ON r.id = i.run_id
        WHERE i.state = 'dismissed'
    """)
    ).mappings():
        evidence = (
            json.loads(row["evidence"]) if isinstance(row["evidence"], str) else row["evidence"]
        )
        if row["kind"] == "repeat_purchase":
            anchor = (evidence.get("record_ids") or [row["source_key"]])[-1]
        elif row["kind"] == "use_cycle_end":
            anchor = evidence.get("use_cycle_id", row["target_uri"])
        else:
            anchor = row["source_key"]
        key = hashlib.sha256(f"{row['kind']}|{row['target_uri']}|{anchor}".encode()).hexdigest()
        if (row["owner_id"], key) in seen:
            continue
        seen.add((row["owner_id"], key))
        connection.execute(
            feedback.insert().values(
                id=uuid.uuid4(),
                owner_id=row["owner_id"],
                episode_key=key,
                state="dismissed",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade():
    op.drop_table("source_observations")
    op.drop_table("suggestion_feedback")
