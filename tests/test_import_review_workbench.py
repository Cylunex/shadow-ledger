from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from app import db as database
from app.models import ArchiveEvidenceLink, AssetBinding, AuditEvent, OutboxEvent


def table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join(
        [
            f"| {' | '.join(headers)} |",
            f"| {' | '.join('---' for _ in headers)} |",
            *(f"| {' | '.join(row)} |" for row in rows),
        ]
    )


def eleme_bill(order_id: str, merchant: str = "原始餐厅", amount: str = "25.00") -> str:
    return table(
        [
            "下单时间",
            "订单号",
            "商户信息",
            "商品及数量",
            "商品描述",
            "订单金额(元)",
            "订单状态",
            "订单子类型",
        ],
        [
            [
                "2026-08-08 12:00:00",
                order_id,
                merchant,
                "1、商品:午餐, 数量:1;",
                "",
                amount,
                "已完成",
                "",
            ]
        ],
    )


def commit_bill(client, headers, order_id: str, key: str):
    return client.post(
        "/api/v1/imports/commit",
        json={"source": {"format": "markdown", "content": eleme_bill(order_id)}},
        headers={**headers, "Idempotency-Key": key},
    )


def test_import_review_learns_and_revokes_exact_rule_without_overwriting_raw_text(
    client, write_headers
):
    first = commit_bill(client, write_headers, "REVIEW-1", "batch-review-1")
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["pending_review_count"] == 1
    item = result["review_items"][0]
    assert item["raw_merchant_name"] == "原始餐厅"
    assert item["raw_item_names"] == ["午餐"]
    assert item["review_reasons"] == ["merchant_confirmation_needed"]

    merchant = client.post(
        "/api/v1/merchants",
        json={"canonical_name": "规范餐厅", "merchant_type": "restaurant"},
        headers={**write_headers, "Idempotency-Key": "merchant-review-1"},
    )
    assert merchant.status_code == 201, merchant.text
    merchant_id = merchant.json()["id"]
    resolved = client.post(
        f"/api/v1/import-reviews/{item['id']}/resolve",
        json={
            "revision": item["revision"],
            "merchant_id": merchant_id,
            "learn_merchant_rule": True,
        },
        headers=write_headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["review_state"] == "resolved"

    record = client.get(
        f"/api/v1/records/{item['record_id']}", headers={"X-Dev-User": "alice"}
    ).json()
    assert record["consumption"]["merchant_name_raw"] == "原始餐厅"
    assert record["consumption"]["merchant_id"] == merchant_id
    assert record["consumption"]["lines"][0]["raw_name"] == "午餐"

    rules = client.get(
        "/api/v1/merchant-normalization-rules?active=true",
        headers={"X-Dev-User": "alice"},
    ).json()["items"]
    assert len(rules) == 1
    assert rules[0]["evidence_count"] == 1
    assert "用户" in rules[0]["explanation"]
    assert "可随时撤销" in rules[0]["explanation"]

    second = commit_bill(client, write_headers, "REVIEW-2", "batch-review-2")
    assert second.status_code == 201, second.text
    assert second.json()["pending_review_count"] == 0
    second_item = second.json()["review_items"][0]
    assert second_item["normalized_merchant_id"] == merchant_id

    revoked = client.post(
        f"/api/v1/merchant-normalization-rules/{rules[0]['id']}/revoke",
        json={"revision": rules[0]["revision"], "reason": "用户修正规则"},
        headers=write_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["active"] is False

    third = commit_bill(client, write_headers, "REVIEW-3", "batch-review-3")
    assert third.status_code == 201, third.text
    assert third.json()["pending_review_count"] == 1
    third_item = third.json()["review_items"][0]
    relearned = client.post(
        f"/api/v1/import-reviews/{third_item['id']}/resolve",
        json={
            "revision": third_item["revision"],
            "merchant_id": merchant_id,
            "learn_merchant_rule": True,
        },
        headers=write_headers,
    )
    assert relearned.status_code == 200, relearned.text
    active_rules = client.get(
        "/api/v1/merchant-normalization-rules?active=true",
        headers={"X-Dev-User": "alice"},
    ).json()["items"]
    assert len(active_rules) == 1
    assert active_rules[0]["id"] != rules[0]["id"]


def test_import_batch_idempotency_rejects_payload_mismatch(client, write_headers):
    first = commit_bill(client, write_headers, "IDEM-1", "stable-import-key")
    replay = commit_bill(client, write_headers, "IDEM-1", "stable-import-key")
    mismatch = commit_bill(client, write_headers, "IDEM-2", "stable-import-key")

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["batch_id"] == first.json()["batch_id"]
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "idempotency_payload_mismatch"


def test_duplicate_rows_in_one_batch_share_fact_and_remain_reviewable(client, write_headers):
    headers = [
        "下单时间",
        "订单号",
        "商户信息",
        "商品及数量",
        "商品描述",
        "订单金额(元)",
        "订单状态",
        "订单子类型",
    ]
    row = [
        "2026-08-08 12:00:00",
        "DUPLICATE-IN-BATCH",
        "原始餐厅",
        "1、商品:午餐, 数量:1;",
        "",
        "25.00",
        "已完成",
        "",
    ]
    response = client.post(
        "/api/v1/imports/commit",
        json={"source": {"format": "markdown", "content": table(headers, [row, row])}},
        headers={**write_headers, "Idempotency-Key": "duplicate-one-batch"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created_count"] == 1
    assert body["duplicate_count"] == 1
    assert len(body["review_items"]) == 2
    assert len(set(body["record_ids"])) == 1
    assert "duplicate" in body["review_items"][1]["review_reasons"]


def test_amount_anomaly_requires_explicit_review_and_keeps_other_reasons_pending(
    client, write_headers
):
    for index, amount in enumerate(("10.00", "11.00", "9.00")):
        response = client.post(
            "/api/v1/records",
            json={
                "occurred_at": f"2026-08-0{index + 1}T12:00:00+08:00",
                "money_entry": {
                    "type": "expense",
                    "amount": amount,
                    "currency": "CNY",
                    "title": "历史午餐",
                },
                "consumption": {
                    "scene": "delivery",
                    "merchant_name_raw": "原始餐厅",
                    "lines": [{"raw_name": "午餐"}],
                },
                "confirm": True,
            },
            headers={**write_headers, "Idempotency-Key": f"history-{index}"},
        )
        assert response.status_code == 201, response.text

    imported = client.post(
        "/api/v1/imports/commit",
        json={
            "source": {
                "format": "markdown",
                "content": eleme_bill("ANOMALY-1", amount="100.00"),
            }
        },
        headers={**write_headers, "Idempotency-Key": "anomaly-batch"},
    )
    assert imported.status_code == 201, imported.text
    item = imported.json()["review_items"][0]
    assert item["amount_anomaly_reason"] == "above_3x_historical_median"
    assert set(item["review_reasons"]) == {
        "amount_anomaly",
        "merchant_confirmation_needed",
    }

    empty = client.post(
        f"/api/v1/import-reviews/{item['id']}/resolve",
        json={"revision": item["revision"]},
        headers=write_headers,
    )
    assert empty.status_code == 422
    accepted = client.post(
        f"/api/v1/import-reviews/{item['id']}/resolve",
        json={"revision": item["revision"], "accept_amount_anomaly": True},
        headers=write_headers,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["review_state"] == "pending"
    assert accepted.json()["review_reasons"] == ["merchant_confirmation_needed"]


def test_refund_match_is_suggested_but_only_linked_after_user_confirmation(
    client, write_headers
):
    expense = client.post(
        "/api/v1/records",
        json={
            "occurred_at": "2026-08-01T12:00:00+08:00",
            "money_entry": {
                "type": "expense",
                "amount": "20.00",
                "currency": "CNY",
                "title": "原消费",
            },
            "confirm": True,
        },
        headers={**write_headers, "Idempotency-Key": "refund-expense"},
    )
    assert expense.status_code == 201, expense.text
    bill = table(
        [
            "交易时间",
            "商户名称",
            "交易说明",
            "金额",
            "交易状态",
            "收/支",
            "交易分类",
            "交易订单号",
            "商家订单号",
        ],
        [
            [
                "2026-08-02 12:00:00",
                "原始商户",
                "退款",
                "20.00",
                "退款成功",
                "收入",
                "其他",
                "REFUND-1",
                "MERCHANT-REFUND-1",
            ]
        ],
    )
    imported = client.post(
        "/api/v1/imports/commit",
        json={"source": {"format": "markdown", "content": bill}},
        headers={**write_headers, "Idempotency-Key": "refund-import"},
    )
    assert imported.status_code == 201, imported.text
    item = imported.json()["review_items"][0]
    assert item["refund_candidate_entry_id"] is not None
    refund_before = client.get(
        f"/api/v1/records/{item['record_id']}", headers={"X-Dev-User": "alice"}
    ).json()
    assert refund_before["money_entry"]["related_entry_id"] is None

    linked = client.post(
        f"/api/v1/import-reviews/{item['id']}/resolve",
        json={"revision": item["revision"], "refund_record_id": expense.json()["id"]},
        headers=write_headers,
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["review_state"] == "resolved"
    refund_after = client.get(
        f"/api/v1/records/{item['record_id']}", headers={"X-Dev-User": "alice"}
    ).json()
    assert refund_after["money_entry"]["related_entry_id"] is not None


def test_archive_evidence_uses_stable_references_and_is_releasable(client, write_headers):
    record = client.post(
        "/api/v1/records",
        json={
            "occurred_at": datetime.now(UTC).isoformat(),
            "money_entry": {
                "type": "expense",
                "amount": "18.50",
                "currency": "CNY",
                "title": "凭证测试",
            },
            "confirm": False,
        },
        headers={**write_headers, "Idempotency-Key": "archive-record"},
    ).json()
    assert database.SessionLocal is not None
    with database.SessionLocal() as session:
        binding = AssetBinding(
            owner_id="alice",
            source_type="record",
            source_id=UUID(record["id"]),
            asset_id=uuid4(),
            asset_reference_id=uuid4(),
            usage="evidence",
        )
        session.add(binding)
        session.commit()
        binding_id = str(binding.id)

    linked = client.post(
        f"/api/v1/records/{record['id']}/archive-evidence",
        json={
            "asset_binding_id": binding_id,
            "archive_uri": "shadow://archive/evidence/example-receipt",
        },
        headers={**write_headers, "Idempotency-Key": "archive-link-1"},
    )
    assert linked.status_code == 201, linked.text
    body = linked.json()
    assert set(body) == {
        "id",
        "record_id",
        "asset_binding_id",
        "archive_uri",
        "active",
        "revision",
    }

    released = client.post(
        f"/api/v1/records/{record['id']}/archive-evidence/{body['id']}/release",
        json={"revision": body["revision"], "reason": "用户解除长期归档关联"},
        headers=write_headers,
    )
    assert released.status_code == 200, released.text
    assert released.json()["active"] is False

    with database.SessionLocal() as session:
        row = session.scalar(select(ArchiveEvidenceLink))
        events = list(session.scalars(select(OutboxEvent)))
        audits = list(session.scalars(select(AuditEvent)))
        assert row is not None and row.archive_uri.startswith("shadow://archive/")
        assert {event.event_type for event in events} >= {
            "ledger.archive_evidence.linked",
            "ledger.archive_evidence.released",
        }
        assert {audit.action for audit in audits} >= {
            "archive.evidence.linked",
            "archive.evidence.released",
        }
        assert "bytes" not in str([event.payload for event in events]).lower()


def test_data_quality_is_descriptive_and_does_not_create_forecast(client):
    response = client.get(
        "/api/v1/insights/data-quality", headers={"X-Dev-User": "alice"}
    )
    assert response.status_code == 200
    readiness = response.json()["prediction_readiness"]
    assert readiness["status"] == "insufficient"
    assert readiness["creates_forecast"] is False
    assert "fewer_than_12_confirmed_consumption_facts" in readiness["blockers"]
