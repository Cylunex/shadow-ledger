"""Run only with an explicitly isolated LEDGER_TEST_POSTGRES_URL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from test_workbench_optimization import post, record

from app import db as database
from app.errors import AppError
from app.models import LedgerRecord, OutboxEvent
from app.schemas import RecordCreate
from app.services.identities import merge_identity
from app.services.records import confirm_record, create_record


@pytest.fixture
def postgres(client):
    if database.engine.dialect.name != "postgresql":
        pytest.skip("requires isolated PostgreSQL")
    return client


def race(actions):
    barrier = Barrier(len(actions))

    def run(action):
        with database.SessionLocal() as db:
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            barrier.wait(timeout=5)
            try:
                return action(db)
            except AppError as error:
                db.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def test_postgres_agent_grant_consumed_once(postgres):
    from app.models import AgentExecutionReceipt, LedgerAgentGrant
    from app.services.agent_effects import approve, execute, request_review
    body = RecordCreate.model_validate({"occurred_at": "2026-09-04T10:00:00+08:00",
        "money_entry": {"type": "expense", "amount": "32.00", "currency": "CNY"}})
    with database.SessionLocal() as db:
        db.add(LedgerAgentGrant(agent_id="agent-test", owner_id="alice", granted_by="alice", allow_confirm=True))
        db.commit()
        row = create_record(db, "alice", body, "agent-concurrency-draft", "agent-test", actor_type="agent")
        intent = request_review(db, "alice", "agent-test", row.id, 1, "confirm", "agent-concurrency-review")
        grant_id = UUID(approve(db, "alice", intent.id, intent.args_hash, True)["approval_grant_id"])
    def commit(db):
        return execute(db, "alice", "agent-test", grant_id)["receipt"]
    results = race([commit, commit])
    assert results[0] == results[1]
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentExecutionReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.event_type == "ledger.record.confirmed")) == 1


def test_postgres_create_same_key_has_one_fact(postgres):
    body = RecordCreate.model_validate(
        {
            "occurred_at": "2026-08-01T12:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "money_entry": {"type": "expense", "amount": "10", "currency": "CNY"},
        }
    )

    def create(db):
        return str(create_record(db, "alice", body, "same-create", "alice").id)

    results = race([create, create])
    assert results[0] == results[1]
    with database.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LedgerRecord)) == 1


def test_postgres_confirm_race_emits_one_outbox(postgres, write_headers):
    row = record(postgres, write_headers)

    def confirm(db):
        return confirm_record(db, "alice", UUID(row["id"]), row["revision"], "alice").state

    results = race([confirm, confirm])
    assert sorted(results) == ["confirmed", "revision_conflict"]
    with database.SessionLocal() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "ledger.record.confirmed")
            )
            == 1
        )


def test_postgres_reverse_merge_cannot_form_cycle(postgres, write_headers):
    first = post(postgres, write_headers, "/merchants", {"canonical_name": "店 A"}).json()["id"]
    second = post(postgres, write_headers, "/merchants", {"canonical_name": "店 B"}).json()["id"]

    def merge(source, target, key):
        return lambda db: str(
            merge_identity(db, "alice", "merchant", UUID(source), UUID(target), "用户核对", key).id
        )

    results = race([merge(first, second, "a-to-b"), merge(second, first, "b-to-a")])
    assert sum(result == "identity_already_merged" for result in results) == 1


def test_postgres_import_same_source_different_batches(postgres, write_headers):
    from test_import_review_workbench import eleme_bill

    from app.api import _commit_markdown_import
    from app.importers import parse_markdown_import
    from app.security import Actor

    # Seed categories before concurrent import; category seeding has its own uniqueness.
    postgres.get("/api/v1/categories", headers={"X-Dev-User": "alice"})
    parsed = parse_markdown_import(eleme_bill("concurrent-source"))

    def importer(key):
        def run(db):
            batch = _commit_markdown_import(
                db, Actor(owner_id="alice", actor_type="user"), parsed, key, {"key": key}
            )
            return batch.created_count

        return run

    assert sorted(race([importer("batch-a"), importer("batch-b")])) == [0, 1]
