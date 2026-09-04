"""Exercises the real 0006→0007 migration, including historic feedback backfill."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app import db as database
from app.models import ForecastItem, ForecastRun, SuggestionFeedback
from app.services.feedback import episode_key


def test_postgres_upgrade_preserves_old_dismissal(client, settings, monkeypatch):
    if database.engine.dialect.name != "postgresql":
        pytest.skip("requires isolated PostgreSQL")
    scope = "ledger_test_migration_" + uuid4().hex
    with database.engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{scope}"'))
    url = database.engine.url.update_query_dict({"options": f"-csearch_path={scope}"})
    temporary = create_engine(url)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: settings.model_copy(
            update={"database_url": SecretStr(url.render_as_string(hide_password=False))}
        ),
    )
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "20260903_0006")
        with Session(temporary) as db:
            run = ForecastRun(
                owner_id="alice",
                as_of=date(2026, 8, 1),
                timezone="Asia/Shanghai",
                horizon_days=90,
                algorithm_version="deterministic-v1",
                input_hash=b"a" * 32,
                output_hash=b"b" * 32,
                input_snapshot={},
            )
            db.add(run)
            db.flush()
            item = ForecastItem(
                run_id=run.id,
                source_key="legacy-source",
                kind="repeat_purchase",
                target_uri="shadow://ledger/items/legacy",
                predicted_at=datetime.now(UTC),
                confidence="0.5",
                explanation="old",
                evidence={"record_ids": ["old-purchase"]},
                expires_at=datetime.now(UTC),
                state="dismissed",
                revision=2,
            )
            db.add(item)
            db.flush()
            expected_key = episode_key(item)
            db.commit()
        command.upgrade(config, "head")
        with Session(temporary) as db:
            row = db.scalar(select(SuggestionFeedback))
            assert row.episode_key == expected_key
            assert row.state == "dismissed"
            assert db.scalar(select(ForecastItem)).state == "dismissed"
        assert "source_observations" in inspect(temporary).get_table_names()
        command.upgrade(config, "head")
    finally:
        temporary.dispose()
        with database.engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{scope}" CASCADE'))
