from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db as database
from app.config import Settings
from app.db import Base
from app.main import create_app
from app.models import (
    AuditEvent,
    BudgetTarget,
    ConsumptionEvent,
    ExternalReference,
    LedgerAgentGrant,
    LedgerRecord,
    MoneyCategory,
    MoneyEntry,
)

AGENT_ID = "ledger-helper"
OWNER_ID = "https://identity.example.com|owner-example"
TOKEN = "ledger-agent-test-token-that-is-long-enough"


@pytest.fixture
def agent_app_factory(settings: Settings, tmp_path: Path):
    @contextmanager
    def factory(
        scopes: tuple[str, ...],
        *,
        grant: bool = True,
        allow_summary: bool = True,
        allow_records: bool = True,
        allow_budgets: bool = True,
        allow_drafts: bool = True,
        allow_confirm: bool = True,
        audiences: tuple[str, ...] = ("ledger",),
    ) -> Iterator[tuple[TestClient, object]]:
        secrets_dir = tmp_path / ("agent-secrets-" + hashlib.sha256(" ".join(scopes).encode()).hexdigest()[:8])
        digest_path = secrets_dir / "agents" / AGENT_ID / "current-token.sha256"
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(hashlib.sha256(TOKEN.encode()).hexdigest(), encoding="utf-8")
        registry_path = secrets_dir / "registry.yaml"
        registry_path.write_text(
            "\n".join(
                [
                    "version: 1",
                    "agents:",
                    f"  {AGENT_ID}:",
                    "    owner_app: ledger",
                    f"    audiences: [{', '.join(audiences)}]",
                    f"    scopes: [{', '.join(scopes)}]",
                    "    credential_hash_files:",
                    f"      - agents/{AGENT_ID}/current-token.sha256",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        resolved = settings.model_copy(
            update={
                "agent_registry_path": registry_path,
                "agent_secrets_dir": secrets_dir,
                "oidc_callbacks": ["https://ledger.example.com/auth/callback"],
                "allowed_origins": ["https://ledger.example.com"],
            }
        )
        app = create_app(resolved, "sqlite+pysqlite:///:memory:")
        with TestClient(app, base_url="https://ledger.example.com") as client:
            assert database.engine is not None
            Base.metadata.create_all(database.engine)
            if grant:
                assert database.SessionLocal is not None
                with database.SessionLocal.begin() as session:
                    session.add(
                        LedgerAgentGrant(
                            agent_id=AGENT_ID,
                            owner_id=OWNER_ID,
                            granted_by="owner-example",
                            allow_summary=allow_summary,
                            allow_records=allow_records,
                            allow_budgets=allow_budgets,
                            allow_drafts=allow_drafts,
                            allow_confirm=allow_confirm,
                        )
                    )
            yield client, app
            Base.metadata.drop_all(database.engine)

    return factory


def _authorization() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_machine_api_accepts_loopback_host_in_production(agent_app_factory) -> None:
    with agent_app_factory(("ledger.summary.read",)) as (client, _app):
        response = client.get(
            "/api/machine/v1/agent/summary",
            headers={**_authorization(), "Host": "127.0.0.1:8000"},
        )
    assert response.status_code == 200


def _seed_financial_facts() -> None:
    assert database.SessionLocal is not None
    occurred_at = datetime(2026, 8, 10, 8, 30, tzinfo=UTC)
    with database.SessionLocal.begin() as session:
        category = MoneyCategory(
            owner_id=OWNER_ID,
            key="food",
            name="餐饮",
            sort_order=1,
            active=True,
        )
        record = LedgerRecord(
            owner_id=OWNER_ID,
            record_kind="money_only",
            state="confirmed",
            occurred_at=occurred_at,
            timezone="Asia/Shanghai",
            note="must-not-reach-agent",
            revision=2,
            confirmed_at=occurred_at,
        )
        session.add_all([category, record])
        session.flush()
        session.add(
            MoneyEntry(
                record=record,
                type="expense",
                amount=Decimal("27.5000"),
                currency="CNY",
                category_id=category.id,
                title="private transaction title",
            )
        )
        session.add(
            BudgetTarget(
                owner_id=OWNER_ID,
                category_id=category.id,
                budget_month=date(2026, 8, 1),
                monthly_amount=Decimal("500.0000"),
                currency="CNY",
                active=True,
                revision=1,
            )
        )


def test_machine_bearer_scope_and_resource_grant_fail_closed(agent_app_factory) -> None:
    with agent_app_factory(("ledger.summary.read",), grant=False) as (client, _):
        missing = client.get("/api/machine/v1/agent/summary?month=2026-08")
        invalid = client.get(
            "/api/machine/v1/agent/summary?month=2026-08",
            headers={"Authorization": "Bearer invalid-test-token-that-is-long-enough"},
        )
        no_grant = client.get(
            "/api/machine/v1/agent/summary?month=2026-08", headers=_authorization()
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"]["code"] == "machine_bearer_required"
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "machine_bearer_invalid"
    assert no_grant.status_code == 404
    assert no_grant.json()["error"]["code"] == "ledger_grant_not_found"

    with agent_app_factory(("ledger.summary.read",), audiences=("travel",)) as (client, _):
        wrong_audience = client.get(
            "/api/machine/v1/agent/summary?month=2026-08", headers=_authorization()
        )

    assert wrong_audience.status_code == 401
    assert wrong_audience.json()["error"]["code"] == "machine_bearer_invalid"

    with agent_app_factory(("ledger.records.read",), allow_records=False) as (client, _):
        denied = client.get(
            "/api/machine/v1/agent/records?month=2026-08", headers=_authorization()
        )
        wrong_scope = client.get(
            "/api/machine/v1/agent/summary?month=2026-08", headers=_authorization()
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ledger_grant_forbidden"
    assert wrong_scope.status_code == 403
    assert wrong_scope.json()["error"]["code"] == "machine_scope_forbidden"


def test_machine_reads_are_minimal_currency_bounded_and_audited(agent_app_factory) -> None:
    scopes = ("ledger.summary.read", "ledger.records.read", "ledger.budgets.read")
    with agent_app_factory(scopes) as (client, _):
        _seed_financial_facts()
        summary = client.get(
            "/api/machine/v1/agent/summary?month=2026-08&currency=CNY",
            headers=_authorization(),
        )
        records = client.get(
            "/api/machine/v1/agent/records?month=2026-08", headers=_authorization()
        )
        budgets = client.get(
            "/api/machine/v1/agent/budgets?month=2026-08", headers=_authorization()
        )

        assert summary.status_code == 200, summary.text
        assert summary.json()["expense"] == "27.5000"
        assert summary.json()["exchange_rate_applied"] is False
        assert records.status_code == 200, records.text
        assert records.json()["items"][0]["amount"] == "27.5000"
        assert records.json()["items"][0]["category_key"] == "food"
        serialized_records = records.text
        assert "private transaction title" not in serialized_records
        assert "must-not-reach-agent" not in serialized_records
        assert "account" not in serialized_records.lower()
        assert "payment" not in serialized_records.lower()
        assert budgets.status_code == 200, budgets.text
        assert budgets.json()["items"][0]["remaining"] == "472.5000"
        assert budgets.json()["exchange_rate_applied"] is False

        assert database.SessionLocal is not None
        with database.SessionLocal() as session:
            audits = list(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.actor_type == "agent").order_by(AuditEvent.action)
                )
            )
        audit_payload = json.dumps([row.details for row in audits], sort_keys=True)
        assert {row.action for row in audits} == {
            "agent.summary.read",
            "agent.records.read",
            "agent.budgets.read",
        }
        assert "27.5" not in audit_payload
        assert "private transaction title" not in audit_payload


def test_agent_draft_is_deterministic_reversible_and_idempotent(agent_app_factory) -> None:
    with agent_app_factory(("ledger.records.draft",)) as (client, _):
        payload = {
            "occurred_at": "2026-08-22T09:30:00+08:00",
            "timezone": "Asia/Shanghai",
            "money_type": "expense",
            "amount": "32.5000",
            "currency": "cny",
            "title": "private draft title",
        }
        headers = {**_authorization(), "Idempotency-Key": "agent-draft-example"}
        created = client.post("/api/machine/v1/agent/drafts", headers=headers, json=payload)
        repeated = client.post("/api/machine/v1/agent/drafts", headers=headers, json=payload)
        changed = client.post(
            "/api/machine/v1/agent/drafts",
            headers=headers,
            json={**payload, "amount": "33.0000"},
        )
        invented_account = client.post(
            "/api/machine/v1/agent/drafts",
            headers={**_authorization(), "Idempotency-Key": "agent-draft-account"},
            json={**payload, "account_id": "not-supported", "exchange_rate": "1.0000"},
        )

        assert created.status_code == repeated.status_code == 201
        assert created.json() == repeated.json()
        assert created.json()["state"] == "draft"
        assert created.json()["reversible"] is True
        assert created.json()["final_entry_created"] is False
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "idempotency_mismatch"
        assert invented_account.status_code == 422

        assert database.SessionLocal is not None
        with database.SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(LedgerRecord)) == 1
            record = session.scalar(select(LedgerRecord))
            audit = session.scalar(
                select(AuditEvent).where(AuditEvent.action == "record.created")
            )
            assert record is not None and record.state == "draft"
            assert record.money_entry is not None
            assert record.money_entry.amount == Decimal("32.5000")
            assert record.money_entry.currency == "CNY"
            assert audit is not None and audit.actor_type == "agent"
            assert set(audit.details) == {"state", "record_kind"}


def test_reviewed_agent_draft_commit_is_scoped_audited_and_idempotent(
    agent_app_factory,
) -> None:
    scopes = ("ledger.records.draft", "ledger.records.write")
    with agent_app_factory(scopes) as (client, _):
        payload = {
            "occurred_at": "2026-08-25T12:10:00+08:00",
            "timezone": "Asia/Shanghai",
            "money_type": "expense",
            "amount": "28.0000",
            "currency": "CNY",
            "title": "午餐",
        }
        created = client.post(
            "/api/machine/v1/agent/drafts",
            headers={**_authorization(), "Idempotency-Key": "reviewed-ledger-draft"},
            json=payload,
        )
        record_id = created.json()["record_ref"].rsplit("/", 1)[-1]
        committed = client.post(
            f"/api/machine/v1/agent/drafts/{record_id}/commit",
            headers=_authorization(),
            json={"revision": created.json()["revision"]},
        )
        repeated = client.post(
            f"/api/machine/v1/agent/drafts/{record_id}/commit",
            headers=_authorization(),
            json={"revision": created.json()["revision"]},
        )

        assert created.status_code == 201
        assert committed.status_code == repeated.status_code == 200
        assert committed.json()["state"] == "confirmed"
        assert committed.json()["revision"] == 2
        assert committed.json()["replayed"] is False
        assert committed.json()["final_entry_created"] is True
        assert repeated.json() == {**committed.json(), "replayed": True}

        assert database.SessionLocal is not None
        with database.SessionLocal() as session:
            record = session.scalar(select(LedgerRecord))
            audit = session.scalar(
                select(AuditEvent).where(AuditEvent.action == "record.confirmed")
            )
            assert record is not None and record.state == "confirmed"
            assert audit is not None
            assert audit.actor_type == "agent"
            assert audit.actor_id == AGENT_ID


def test_agent_draft_commit_requires_write_scope_and_resource_grant(agent_app_factory) -> None:
    with agent_app_factory(("ledger.records.draft",)) as (client, _):
        created = client.post(
            "/api/machine/v1/agent/drafts",
            headers={**_authorization(), "Idempotency-Key": "scope-denied-draft"},
            json={
                "occurred_at": "2026-08-25T12:10:00+08:00",
                "money_type": "expense",
                "amount": "18.0000",
                "currency": "CNY",
            },
        )
        record_id = created.json()["record_ref"].rsplit("/", 1)[-1]
        denied = client.post(
            f"/api/machine/v1/agent/drafts/{record_id}/commit",
            headers=_authorization(),
            json={"revision": 1},
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "machine_scope_forbidden"

    with agent_app_factory(
        ("ledger.records.draft", "ledger.records.write"), allow_confirm=False
    ) as (client, _):
        created = client.post(
            "/api/machine/v1/agent/drafts",
            headers={**_authorization(), "Idempotency-Key": "grant-denied-draft"},
            json={
                "occurred_at": "2026-08-25T12:10:00+08:00",
                "money_type": "expense",
                "amount": "18.0000",
                "currency": "CNY",
            },
        )
        record_id = created.json()["record_ref"].rsplit("/", 1)[-1]
        denied = client.post(
            f"/api/machine/v1/agent/drafts/{record_id}/commit",
            headers=_authorization(),
            json={"revision": 1},
        )

    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ledger_grant_forbidden"


def test_pending_agent_drafts_can_be_federated_and_rejected_from_nexus(
    agent_app_factory,
) -> None:
    with agent_app_factory(("ledger.records.draft", "ledger.records.write")) as (client, _):
        created = client.post(
            "/api/machine/v1/agent/drafts",
            headers={**_authorization(), "Idempotency-Key": "federated-ledger-draft"},
            json={
                "occurred_at": "2026-08-25T12:10:00+08:00",
                "money_type": "expense",
                "amount": "139.6300",
                "currency": "CNY",
                "category_key": None,
                "title": "待审核超市记录",
            },
        )
        record_id = created.json()["record_ref"].rsplit("/", 1)[-1]
        pending = client.get("/api/machine/v1/agent/drafts", headers=_authorization())
        rejected = client.post(
            f"/api/machine/v1/agent/drafts/{record_id}/reject",
            headers=_authorization(),
            json={"revision": 1},
        )
        remaining = client.get("/api/machine/v1/agent/drafts", headers=_authorization())

        assert pending.status_code == 200
        assert len(pending.json()["items"]) == 1
        item = pending.json()["items"][0]
        assert item["record_ref"] == created.json()["record_ref"]
        assert item["revision"] == 1
        assert item["occurred_at"].startswith("2026-08-25T12:10:00")
        assert item["money_type"] == "expense"
        assert item["amount"] == "139.6300"
        assert item["currency"] == "CNY"
        assert item["category_key"] is None
        assert item["title"] == "待审核超市记录"
        assert rejected.status_code == 200
        assert rejected.json()["state"] == "rejected"
        assert rejected.json()["replayed"] is False
        assert remaining.json()["items"] == []

        replayed = client.post(
            f"/api/machine/v1/agent/drafts/{record_id}/reject",
            headers=_authorization(),
            json={"revision": 1},
        )
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True

        assert database.SessionLocal is not None
        with database.SessionLocal() as session:
            assert session.get(LedgerRecord, uuid.UUID(record_id)) is None
            rejected_audit = session.scalar(
                select(AuditEvent).where(AuditEvent.action == "record.draft_rejected")
            )
            assert rejected_audit is not None and rejected_audit.actor_type == "agent"


def test_standard_nexus_review_protocol_creates_lists_and_commits(agent_app_factory) -> None:
    with agent_app_factory(("ledger.records.draft", "ledger.records.write")) as (client, _):
        created = client.post(
            "/api/machine/v1/agent/nexus/reviews",
            headers={**_authorization(), "Idempotency-Key": "nexus-ledger-review"},
            json={
                "intent": "ledger.record",
                "summary": "午餐",
                "fields": {
                    "occurredAt": "2026-08-26T12:30:00+08:00",
                    "moneyType": "expense",
                    "amount": "36.5000",
                    "currency": "CNY",
                    "title": "午餐",
                },
            },
        )
        assert created.status_code == 201, created.text
        review = created.json()
        assert review["protocol"] == "shadow.review.v1"
        assert review["domain"] == "ledger"
        assert review["state"] == "pending"
        assert review["fields"]["amount"] == "36.5000"

        listed = client.get(
            "/api/machine/v1/agent/nexus/reviews", headers=_authorization()
        )
        assert listed.status_code == 200, listed.text
        assert [item["review_id"] for item in listed.json()["items"]] == [review["review_id"]]

        committed = client.post(
            f"/api/machine/v1/agent/nexus/reviews/{review['review_id']}/commit",
            headers=_authorization(),
            json={"revision": review["revision"]},
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["state"] == "committed"
        assert committed.json()["receipt"] == review["reference"]


def test_nexus_review_preserves_rich_consumption_and_source_refs(agent_app_factory) -> None:
    with agent_app_factory(("ledger.records.draft", "ledger.records.write")) as (
        client,
        _,
    ):
        assert database.SessionLocal is not None
        with database.SessionLocal.begin() as session:
            session.add(MoneyCategory(owner_id=OWNER_ID, key="food", name="餐饮"))

        payload = {
            "intent": "ledger.transaction",
            "summary": "黄焖鸡外卖午餐",
            "fields": {
                "occurredAt": "2026-09-01T11:52:08+08:00",
                "moneyType": "expense",
                "amount": "34.9000",
                "currency": "CNY",
                "categoryKey": "food",
                "title": "黄焖鸡外卖午餐",
                "scene": "delivery",
                "merchantNameRaw": "黄焖焖黄焖鸡米饭（百子湾店）",
                "channelNameRaw": "外卖",
                "consumptionNote": "实付金额来自订单截图。",
                "consumptionItemsJson": json.dumps(
                    [
                        {"rawName": "黄焖鸡大份+鱼豆腐+米饭套餐", "sortOrder": 0},
                        {"rawName": "金针菇", "quantity": "1", "unit": "份", "sortOrder": 1},
                    ],
                    ensure_ascii=False,
                ),
            },
            "source_refs": [
                "shadow://nexus/health/meals/2026-09-01/lunch/meal-photo",
                "shadow://nexus/health/meals/2026-09-01/lunch/order-items",
            ],
        }
        headers = {**_authorization(), "Idempotency-Key": "nexus-rich-ledger-review"}
        created = client.post("/api/machine/v1/agent/nexus/reviews", headers=headers, json=payload)
        assert created.status_code == 201, created.text
        review = created.json()
        assert review["fields"]["scene"] == "delivery"
        assert review["fields"]["merchantNameRaw"].startswith("黄焖焖")
        assert [item["rawName"] for item in review["fields"]["consumptionItemsJson"]] == [
            "黄焖鸡大份+鱼豆腐+米饭套餐",
            "金针菇",
        ]
        assert review["source_refs"] == sorted(payload["source_refs"])

        record_id = uuid.UUID(review["review_id"])
        with database.SessionLocal() as session:
            record = session.get(LedgerRecord, record_id)
            consumption = session.scalar(
                select(ConsumptionEvent).where(ConsumptionEvent.record_id == record_id)
            )
            refs = set(
                session.scalars(
                    select(ExternalReference.target_uri).where(
                        ExternalReference.source_id == record_id
                    )
                )
            )
            assert record is not None and record.record_kind == "consumption"
            assert consumption is not None and consumption.scene == "delivery"
            assert [line.raw_name for line in consumption.lines] == [
                "黄焖鸡大份+鱼豆腐+米饭套餐",
                "金针菇",
            ]
            assert refs == set(payload["source_refs"])

        changed_refs = {
            **payload,
            "source_refs": ["shadow://nexus/health/meals/2026-09-01/lunch/other"],
        }
        mismatch = client.post(
            "/api/machine/v1/agent/nexus/reviews",
            headers=headers,
            json=changed_refs,
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "idempotency_mismatch"

        committed = client.post(
            f"/api/machine/v1/agent/nexus/reviews/{record_id}/commit",
            headers=_authorization(),
            json={"revision": review["revision"]},
        )
        assert committed.status_code == 200, committed.text
        assert committed.json()["state"] == "committed"
        assert committed.json()["fields"]["scene"] == "delivery"
        assert committed.json()["source_refs"] == sorted(payload["source_refs"])


def test_model_visible_agent_draft_remains_money_only(agent_app_factory) -> None:
    with agent_app_factory(("ledger.records.draft",)) as (client, _):
        response = client.post(
            "/api/machine/v1/agent/drafts",
            headers={**_authorization(), "Idempotency-Key": "money-only-boundary"},
            json={
                "occurred_at": "2026-09-01T11:52:08+08:00",
                "money_type": "expense",
                "amount": "34.9000",
                "currency": "CNY",
                "consumption": {"scene": "delivery"},
            },
        )
        assert response.status_code == 422
