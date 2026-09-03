from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from decimal import Decimal
from typing import get_args

import pytest

from app.mcp_server import mcp_create_draft, mcp_records
from app.payments import PAYMENT_METHOD_LABELS, PaymentMethod, parse_payment_method
from app.schemas import RecordCreate, jsonable
from app.services.records import request_hash


@pytest.mark.parametrize("format", ["json", "csv"])
def test_export_preserves_method_without_changing_amount(
    client, idempotent_headers, monkeypatch, format
):
    from app import db as database
    from app.models import BackgroundJob
    from app.worker import process_job

    client.post("/api/v1/records", json=payload(), headers=idempotent_headers).raise_for_status()
    captured = {}

    def store_export(_self, owner, filename, mime, content, key):
        captured["content"] = content
        return uuid.uuid4()

    monkeypatch.setattr("app.worker.AssetClient.store_export", store_export)
    with database.SessionLocal() as session:
        process_job(
            session,
            BackgroundJob(
                id=uuid.uuid4(), job_type="export", payload={"owner_id": "alice", "format": format}
            ),
        )
    if format == "csv":
        row = list(csv.DictReader(io.StringIO(captured["content"].decode("utf-8-sig"))))[0]
    else:
        row = json.loads(captured["content"])["records"][0]["money_entry"]
    assert row["payment_method"] == "gift_card"
    assert row["amount"] == "23.4200"


def test_markdown_import_keeps_payment_tag_and_source(client, idempotent_headers):
    from app.importers import parse_markdown_import

    text = "\n".join(
        [
            "| 交易时间 | 商户名称 | 交易说明 | 金额 | 收/付款方式 | 交易状态 | 收/支 | 交易分类 | 交易订单号 | 商家订单号 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            "| 2026-09-03 08:41:34 | 示例商店 | 牙刷头 | 38.86 | 白条 | 交易成功 | 支出 | 日用 | SAMPLE-T | SAMPLE-M |",
        ]
    )
    parsed = parse_markdown_import(text)
    candidate = parsed.candidates[0]
    assert candidate.record.money_entry.payment_method == "jd_baitiao"
    assert candidate.record.money_entry.amount == Decimal("38.86")
    assert candidate.raw_payload["rows"][0]["收/付款方式"] == "白条"
    client.post(
        "/api/v1/categories",
        json={"key": candidate.record.money_entry.category_key, "name": "购物"},
        headers={**idempotent_headers, "Idempotency-Key": "payment-category"},
    ).raise_for_status()
    response = client.post(
        "/api/v1/records", json=candidate.record.model_dump(mode="json"), headers=idempotent_headers
    )
    assert response.status_code == 201
    assert response.json()["money_entry"]["payment_method"] == "jd_baitiao"
    assert response.json()["state"] == "draft"


def payload(method="gift_card"):
    return {
        "occurred_at": "2026-09-03T08:37:26+08:00",
        "confirm": True,
        "money_entry": {
            "type": "expense",
            "amount": "23.42",
            "currency": "CNY",
            "title": "洗发露",
            "payment_method": method,
        },
        "consumption": {
            "scene": "online_purchase",
            "lines": [{"raw_name": "洗发露", "quantity": "1", "unit": "瓶", "note": "600ml"}],
        },
    }


def test_payment_roundtrip_filter_patch_compatibility(client, idempotent_headers):
    client.headers["X-Dev-User"] = "alice"
    created = client.post("/api/v1/records", json=payload(), headers=idempotent_headers)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["money_entry"]["payment_method"] == "gift_card"
    assert record["money_entry"]["amount"] == "23.4200"
    path = f"/api/v1/records/{record['id']}"
    assert client.get(path).json()["money_entry"] == record["money_entry"]
    for query in ("payment_method=gift_card", "query=礼品卡"):
        assert len(client.get(f"/api/v1/records?{query}").json()["items"]) == 1
    assert not client.get("/api/v1/records?payment_method=alipay").json()["items"]
    assert not client.get(
        "/api/v1/records?payment_method=gift_card", headers={"X-Dev-User": "bob"}
    ).json()["items"]
    assert client.get("/api/v1/records?payment_method=招商银行1234").status_code == 422
    replay = client.post("/api/v1/records", json=payload(), headers=idempotent_headers)
    assert replay.json()["id"] == record["id"]
    assert (
        client.post("/api/v1/records", json=payload("cash"), headers=idempotent_headers).status_code
        == 409
    )

    # An old client resubmits the money fields without knowing payment_method.
    money = payload()["money_entry"]
    money.pop("payment_method")
    patch = {"money_entry": money, "correction_reason": "更新标题，保留支付方式"}
    headers = {**idempotent_headers, "If-Match": str(record["revision"])}
    updated = client.patch(path, json=patch, headers=headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["money_entry"]["payment_method"] == "gift_card"
    assert client.patch(path, json=patch, headers=headers).status_code == 409
    money["payment_method"] = None
    headers["If-Match"] = str(updated.json()["revision"])
    cleared = client.patch(path, json=patch, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["money_entry"]["payment_method"] is None
    assert cleared.json()["money_entry"]["amount"] == "23.4200"
    assert cleared.json()["consumption"] == record["consumption"]


@pytest.mark.parametrize(
    "method",
    ["支付宝", "招商银行尾号1234", "1234567890123456", {"account": "x"}, ["cash", "gift_card"]],
)
def test_payment_rejects_free_text_accounts_and_allocations(client, idempotent_headers, method):
    assert (
        client.post("/api/v1/records", json=payload(method), headers=idempotent_headers).status_code
        == 422
    )


def test_legacy_hash_compatible_and_optional_method():
    legacy = jsonable(RecordCreate.model_validate(payload(None)).model_dump())
    legacy["money_entry"].pop("payment_method")
    old_hash = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    current = RecordCreate.model_validate(legacy).model_dump()
    assert request_hash(current) == old_hash
    assert request_hash({"records": [current]}) == request_hash({"records": [legacy]})
    current["money_entry"]["payment_method"] = "cash"
    assert request_hash(current) != old_hash


def test_payment_labels_and_import_conservative():
    assert set(PAYMENT_METHOD_LABELS) == set(get_args(PaymentMethod))
    for key, label in PAYMENT_METHOD_LABELS.items():
        assert parse_payment_method(label) == key
    assert parse_payment_method("白条") == "jd_baitiao"
    for raw in (None, "", "在线支付", "某银行1234", "京东商城"):
        assert parse_payment_method(raw) is None


def test_mcp_method_preservation_and_no_confirmation(client, idempotent_headers):
    client.headers["X-Dev-User"] = "alice"
    client.post("/api/v1/records", json=payload(), headers=idempotent_headers).raise_for_status()
    rows = mcp_records("alice", "2026-09", 50, "gift_card")["items"]
    assert len(rows) == 1 and rows[0]["payment_method"] == "gift_card"
    assert not mcp_records("bob", "2026-09", 50, "gift_card")["items"]
    result = mcp_create_draft(
        "alice",
        idempotency_key="payment-method-test",
        money_type="expense",
        amount="3",
        currency="CNY",
        payment_method="alipay",
    )
    assert result["state"] == "draft"
    record_id = result["uri"].rsplit("/", 1)[1]
    assert (
        client.get(f"/api/v1/records/{record_id}").json()["money_entry"]["payment_method"]
        == "alipay"
    )


def test_payment_options_and_ui_delivery(client):
    options = client.get("/api/v1/payment-methods").json()["items"]
    assert {x["key"]: x["label"] for x in options} == PAYMENT_METHOD_LABELS
    page = client.get("/").text
    assert 'id="payment-method"' in page and 'id="filter-payment"' in page
