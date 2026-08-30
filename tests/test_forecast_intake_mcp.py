from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client
from sqlalchemy import func, select

from app import db as database
from app.config import Settings
from app.db import Base
from app.mcp_server import build_mcp_server, mcp_create_draft, mcp_summary
from app.models import CaptureSource, ForecastRun, LedgerRecord
from app.services.forecast import calculate
from app.services.intake import process_intake_directory
from app.worker import generate_daily_forecasts


def create_item(client, headers, name: str = "滤芯") -> str:
    response = client.post(
        "/api/v1/items",
        headers={**headers, "Idempotency-Key": f"item-{len(name)}-{name.encode().hex()[:20]}"},
        json={
            "kind": "product",
            "canonical_name": name,
            "brand": None,
            "variant": None,
            "merchant_id": None,
            "barcode": None,
            "external_ids": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_consumption(client, headers, item_id: str, occurred_at: str, key: str) -> str:
    response = client.post(
        "/api/v1/records",
        headers={**headers, "Idempotency-Key": key},
        json={
            "occurred_at": occurred_at,
            "timezone": "Asia/Shanghai",
            "note": "",
            "money_entry": {
                "type": "expense",
                "amount": "30.00",
                "currency": "CNY",
                "category_key": None,
                "title": "更换滤芯",
                "related_entry_id": None,
            },
            "consumption": {
                "scene": "online_purchase",
                "merchant_id": None,
                "merchant_name_raw": "原始店铺",
                "channel_key": None,
                "channel_name_raw": "原始渠道",
                "place_ref": None,
                "rating": None,
                "would_repeat": True,
                "note": "",
                "lines": [
                    {
                        "raw_name": "原始滤芯文本",
                        "item_identity_id": item_id,
                        "quantity": "1",
                        "unit": "个",
                        "amount": "30.00",
                        "content_category": None,
                        "note": "",
                        "sort_order": 0,
                    }
                ],
            },
            "confirm": True,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "confirmed"
    return response.json()["id"]


def test_use_cycle_is_explicit_versioned_fact(client, write_headers):
    item_id = create_item(client, write_headers)
    draft = client.post(
        "/api/v1/records",
        headers={**write_headers, "Idempotency-Key": "cycle-source-draft"},
        json={
            "occurred_at": "2026-08-01T10:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "note": "",
            "money_entry": {
                "type": "expense",
                "amount": "10",
                "currency": "CNY",
                "title": "draft",
            },
            "consumption": None,
            "confirm": False,
        },
    ).json()
    payload = {
        "item_identity_id": item_id,
        "source_record_id": draft["id"],
        "label": "净水器滤芯",
        "started_at": "2026-08-02T10:00:00+08:00",
        "expected_end_at": "2026-10-02T10:00:00+08:00",
        "note": "手工声明开始使用",
    }
    cross_owner = client.post(
        "/api/v1/use-cycles",
        headers={
            **write_headers,
            "X-Dev-User": "bob",
            "Idempotency-Key": "cycle-cross-owner",
        },
        json={**payload, "source_record_id": None},
    )
    assert cross_owner.status_code == 404
    rejected = client.post(
        "/api/v1/use-cycles",
        headers={**write_headers, "Idempotency-Key": "cycle-1"},
        json=payload,
    )
    assert rejected.status_code == 422
    payload["source_record_id"] = None
    created = client.post(
        "/api/v1/use-cycles",
        headers={**write_headers, "Idempotency-Key": "cycle-1"},
        json=payload,
    )
    assert created.status_code == 201
    cycle = created.json()
    assert cycle["state"] == "active"
    replay = client.post(
        "/api/v1/use-cycles",
        headers={**write_headers, "Idempotency-Key": "cycle-1"},
        json=payload,
    )
    assert replay.json()["id"] == cycle["id"]
    ended = client.post(
        f"/api/v1/use-cycles/{cycle['id']}/complete",
        headers={**write_headers, "If-Match": '"1"'},
        json={"ended_at": "2026-09-01T10:00:00+08:00"},
    )
    assert ended.status_code == 200
    assert ended.json()["state"] == "completed"
    conflict = client.post(
        f"/api/v1/use-cycles/{cycle['id']}/cancel",
        headers={**write_headers, "If-Match": '"1"'},
        json={"ended_at": None},
    )
    assert conflict.status_code == 409


def test_deterministic_forecast_replays_and_never_creates_facts(client, write_headers):
    item_id = create_item(client, write_headers, "咖啡豆")
    for index, occurred_at in enumerate(
        [
            "2026-06-01T08:00:00+00:00",
            "2026-07-01T08:00:00+00:00",
            "2026-08-01T08:00:00+00:00",
        ]
    ):
        create_consumption(client, write_headers, item_id, occurred_at, f"forecast-record-{index}")
    create_consumption(
        client,
        write_headers,
        item_id,
        "2026-10-01T08:00:00+00:00",
        "forecast-future-record",
    )
    commitment = client.post(
        "/api/v1/recurring-commitments",
        headers={**write_headers, "Idempotency-Key": "forecast-commitment"},
        json={
            "kind": "subscription",
            "title": "云服务",
            "merchant_id": None,
            "item_identity_id": None,
            "expected_amount": "20.00",
            "currency": "CNY",
            "recurrence_rule": "FREQ=MONTHLY",
            "timezone": "Asia/Shanghai",
            "next_due_at": "2026-09-10T00:00:00+00:00",
            "auto_renew": True,
            "remind_before_seconds": 86400,
        },
    )
    assert commitment.status_code == 201
    cycle = client.post(
        "/api/v1/use-cycles",
        headers={**write_headers, "Idempotency-Key": "forecast-cycle"},
        json={
            "item_identity_id": item_id,
            "source_record_id": None,
            "label": "本袋咖啡豆",
            "started_at": "2026-08-15T00:00:00+00:00",
            "expected_end_at": "2026-09-15T00:00:00+00:00",
            "note": "",
        },
    )
    assert cycle.status_code == 201

    first = client.post(
        "/api/v1/forecasts/generate",
        headers=write_headers,
        json={"as_of": "2026-09-01", "horizon_days": 90},
    )
    assert first.status_code == 201, first.text
    data = first.json()
    assert {item["kind"] for item in data["items"]} == {
        "commitment_due",
        "repeat_purchase",
        "use_cycle_end",
    }
    repeat = next(item for item in data["items"] if item["kind"] == "repeat_purchase")
    assert repeat["evidence"]["sample_count"] == 3
    assert data["replayed"] is False
    second = client.post(
        "/api/v1/forecasts/generate",
        headers=write_headers,
        json={"as_of": "2026-09-01", "horizon_days": 90},
    ).json()
    assert second["id"] == data["id"]
    assert second["output_hash"] == data["output_hash"]
    assert second["replayed"] is True
    verified = client.post(f"/api/v1/forecasts/{data['id']}/verify", headers=write_headers)
    assert verified.json()["verified"] is True
    confirmed = client.get(
        "/api/v1/records?state=confirmed&limit=20", headers=write_headers
    ).json()["items"]
    assert len(confirmed) == 4


def test_forecast_learns_use_duration_without_turning_it_into_a_fact():
    snapshot = {
        "as_of": "2026-09-01",
        "timezone": "Asia/Shanghai",
        "horizon_days": 30,
        "commitments": [],
        "purchases": [],
        "use_cycles": [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "item_identity_id": "00000000-0000-0000-0000-000000000010",
                "state": "completed",
                "started_at": "2026-07-01T00:00:00+08:00",
                "expected_end_at": None,
                "ended_at": "2026-07-11T00:00:00+08:00",
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "item_identity_id": "00000000-0000-0000-0000-000000000010",
                "state": "completed",
                "started_at": "2026-08-01T00:00:00+08:00",
                "expected_end_at": None,
                "ended_at": "2026-08-13T00:00:00+08:00",
            },
            {
                "id": "00000000-0000-0000-0000-000000000003",
                "item_identity_id": "00000000-0000-0000-0000-000000000010",
                "state": "active",
                "started_at": "2026-08-25T00:00:00+08:00",
                "expected_end_at": None,
                "ended_at": None,
            },
        ],
    }
    result = calculate(snapshot)
    assert len(result) == 1
    assert result[0]["kind"] == "use_cycle_end"
    assert result[0]["predicted_at"] == "2026-09-03T16:00:00+00:00"
    assert result[0]["evidence"] == {
        "use_cycle_id": "00000000-0000-0000-0000-000000000003",
        "sample_count": 2,
        "duration_seconds": 864000,
    }


def test_worker_generates_one_daily_forecast_without_creating_facts(client, write_headers):
    item_id = create_item(client, write_headers, "自动预测样本")
    create_consumption(
        client,
        write_headers,
        item_id,
        "2026-07-01T08:00:00+00:00",
        "auto-forecast-record",
    )
    assert database.SessionLocal is not None
    with database.SessionLocal() as session:
        fact_count = session.scalar(select(func.count()).select_from(LedgerRecord))
        assert generate_daily_forecasts(session) == 1
        assert generate_daily_forecasts(session) == 0
        assert session.scalar(select(func.count()).select_from(ForecastRun)) == 1
        assert session.scalar(select(func.count()).select_from(LedgerRecord)) == fact_count


def structured_payload(*, confirm: bool = False, amount: str = "18.50") -> dict:
    return {
        "source_external_id": "order-1001",
        "captured_at": "2026-08-30T12:00:00+08:00",
        "metadata": {"provider": "example", "original_status": "paid"},
        "records": [
            {
                "occurred_at": "2026-08-30T12:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "note": "",
                "money_entry": {
                    "type": "expense",
                    "amount": amount,
                    "currency": "CNY",
                    "title": "原始订单标题",
                },
                "consumption": None,
                "confirm": confirm,
            }
        ],
    }


def test_webhook_intake_is_draft_only_idempotent_and_credential_safe(client, write_headers):
    created = client.post(
        "/api/v1/intake/webhooks/example",
        headers=write_headers,
        json=structured_payload(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["replayed"] is False
    record = client.get(
        f"/api/v1/records/{created.json()['record_ids'][0]}", headers=write_headers
    ).json()
    assert record["state"] == "draft"
    replay = client.post(
        "/api/v1/intake/webhooks/example", headers=write_headers, json=structured_payload()
    )
    assert replay.json()["replayed"] is True
    changed = client.post(
        "/api/v1/intake/webhooks/example",
        headers=write_headers,
        json=structured_payload(amount="19.00"),
    )
    assert changed.status_code == 409
    confirmed = client.post(
        "/api/v1/intake/webhooks/example",
        headers=write_headers,
        json=structured_payload(confirm=True),
    )
    assert confirmed.status_code == 422
    unsafe = structured_payload()
    unsafe["source_external_id"] = "order-unsafe"
    unsafe["metadata"] = {"access_token": "must-not-be-stored"}
    assert (
        client.post(
            "/api/v1/intake/webhooks/example", headers=write_headers, json=unsafe
        ).status_code
        == 422
    )


def test_directory_intake_moves_processed_envelope(client, tmp_path: Path):
    envelope = {"adapter": "receipt", "payload": structured_payload()}
    incoming = tmp_path / "receipt.json"
    incoming.write_text(json.dumps(envelope), encoding="utf-8")
    assert database.SessionLocal is not None
    with database.SessionLocal() as session:
        result = process_intake_directory(session, tmp_path, "alice", settle_seconds=0, limit=10)
        assert result == {"processed": 1, "failed": 0, "skipped": 0}
        source = session.scalar(select(CaptureSource).where(CaptureSource.owner_id == "alice"))
        assert source is not None
        assert source.raw_payload["intake"]["metadata"]["original_status"] == "paid"
    assert not incoming.exists()
    assert len(list((tmp_path / "processed").glob("*.json"))) == 1


def test_mcp_stdio_exposes_read_and_explicit_draft_only_tools(tmp_path: Path):
    database_path = tmp_path / "mcp.db"
    owner_path = tmp_path / "owner-id"
    owner_path.write_text("alice", encoding="utf-8")
    settings = Settings(
        env="test",
        database_url=f"sqlite+pysqlite:///{database_path}",
        oidc_issuer="https://identity.example.invalid",
        oidc_client_secret="test",
        session_secret="test-session-secret",
        mcp_owner_id_file=owner_path,
        mcp_allow_drafts=True,
    )
    server = build_mcp_server(settings)
    assert database.engine is not None
    Base.metadata.create_all(database.engine)
    draft = mcp_create_draft(
        "alice",
        idempotency_key="mcp-draft-1",
        money_type="expense",
        amount="12.30",
        currency="cny",
        title="MCP 草稿",
    )
    assert draft["state"] == "draft"
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(LedgerRecord)) == 1
        assert session.scalar(select(LedgerRecord.state)) == "draft"
    assert mcp_summary("alice", "2026-08", "CNY")["expense"] == "0.0000"

    async def exercise_protocol() -> tuple[set[str], dict, dict]:
        async with Client(server) as mcp_client:
            result = await mcp_client.list_tools()
            summary = await mcp_client.call_tool(
                "ledger_monthly_summary", {"month": "2026-08", "currency": "CNY"}
            )
            created = await mcp_client.call_tool(
                "ledger_create_draft",
                {
                    "idempotency_key": "mcp-protocol-draft",
                    "money_type": "income",
                    "amount": "88.00",
                    "currency": "CNY",
                    "title": "协议草稿",
                },
            )
            assert not summary.is_error and not created.is_error
            return (
                {tool.name for tool in result.tools},
                summary.structured_content or {},
                created.structured_content or {},
            )

    names, summary, created = asyncio.run(exercise_protocol())
    assert names == {
        "ledger_monthly_summary",
        "ledger_records",
        "ledger_forecasts",
        "ledger_create_draft",
    }
    assert summary["net_spending"] == "0.0000"
    assert created["state"] == "draft"
    with database.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(LedgerRecord)) == 2
        assert set(session.scalars(select(LedgerRecord.state))) == {"draft"}

    readonly_server = build_mcp_server(settings.model_copy(update={"mcp_allow_drafts": False}))

    async def readonly_tool_names() -> set[str]:
        async with Client(readonly_server) as mcp_client:
            result = await mcp_client.list_tools()
            return {tool.name for tool in result.tools}

    assert "ledger_create_draft" not in asyncio.run(readonly_tool_names())
