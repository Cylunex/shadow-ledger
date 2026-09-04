from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_forecast_intake_mcp import create_item
from test_import_review_workbench import commit_bill, eleme_bill

from app import db as database
from app.models import (
    CaptureSource,
    ImportReviewItem,
    LedgerRecord,
    SourceObservation,
    SuggestionFeedback,
)
from app.services.forecast import build_input_snapshot
from app.services.payment_imports import parse_payment_import

API = "/api/v1"
READ = {"X-Dev-User": "alice"}


def post(client, headers, path, body=None, key=None):
    return client.post(
        API + path, headers={**headers, "Idempotency-Key": key or str(uuid4())}, json=body
    )


def record(
    client,
    headers,
    *,
    amount="30",
    kind="expense",
    confirmed=False,
    item=None,
    extra=False,
    day="2026-08-01",
    merchant="同一家",
    currency="CNY",
):
    body = {
        "occurred_at": day + "T12:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "money_entry": {"type": kind, "amount": amount, "currency": currency, "title": merchant},
        "consumption": {
            "scene": "online_purchase",
            "merchant_name_raw": merchant,
            "lines": [{"raw_name": "原始商品", "item_identity_id": item}]
            + ([{"raw_name": "未识别商品"}] if extra else []),
        },
        "confirm": confirmed,
    }
    response = post(client, headers, "/records", body)
    assert response.status_code == 201, response.text
    return response.json()


def generate(client, headers, day):
    response = post(
        client,
        headers,
        "/forecasts/generate",
        {"as_of": day, "timezone": "Asia/Shanghai", "horizon_days": 90},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_workbench_projection_paginates_and_isolates_owners(client, write_headers):
    first = record(client, write_headers)
    record(client, {**write_headers, "X-Dev-User": "bob"})
    imported = commit_bill(client, write_headers, "projection", "projection")
    assert imported.status_code == 201
    with database.SessionLocal() as db:
        db.add(
            CaptureSource(
                owner_id="alice",
                source_type="asset",
                capture_state="failed",
                error_code="parser_failed",
            )
        )
        db.commit()
    results = []
    cursor = None
    while True:
        response = client.get(
            API + "/workbench",
            params={"limit": 1, **({"cursor": cursor} if cursor else {})},
            headers=READ,
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 3
        results.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert {row["kind"] for row in results} == {"draft", "review", "failed"}
    assert len({row["id"] for row in results}) == 3
    assert first["id"] in {row["target_id"] for row in results}
    assert client.get(API + "/workbench?cursor=invalid", headers=READ).status_code == 422


def test_partial_refund_candidates_are_explained_but_not_applied(client, write_headers):
    right = record(client, write_headers, amount="100", confirmed=True)
    record(client, write_headers, amount="20", confirmed=True, merchant="别的店")
    refund = record(client, write_headers, amount="20", kind="refund", day="2026-08-02")
    response = client.get(API + f"/records/{refund['id']}/refund-candidates", headers=READ)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items[0]["record_id"] == right["id"]
    assert "same_raw_merchant" in items[0]["reasons"]
    assert "partial_refund_possible" in items[0]["reasons"]
    assert refund["money_entry"]["related_entry_id"] is None
    assert (
        client.get(
            API + f"/records/{refund['id']}/refund-candidates", headers={"X-Dev-User": "bob"}
        ).status_code
        == 404
    )


def test_review_and_confirmation_are_one_versioned_decision(client, write_headers):
    imported = commit_bill(client, write_headers, "confirm-review", "confirm-review").json()
    item = imported["review_items"][0]
    row = client.get(API + f"/records/{item['record_id']}", headers=READ).json()
    payload = {
        "revision": item["revision"],
        "record_revision": row["revision"],
        "keep_merchant_unknown": True,
        "confirm": True,
    }
    response = post(client, write_headers, f"/import-reviews/{item['id']}/resolve", payload)
    assert response.status_code == 200, response.text
    assert client.get(API + f"/records/{row['id']}", headers=READ).json()["state"] == "confirmed"
    assert (
        post(client, write_headers, f"/import-reviews/{item['id']}/resolve", payload).status_code
        == 409
    )


def test_stale_review_cannot_confirm_modified_record(client, write_headers):
    item = commit_bill(client, write_headers, "stale", "stale").json()["review_items"][0]
    response = client.patch(
        API + f"/records/{item['record_id']}",
        headers={**write_headers, "If-Match": '"1"'},
        json={"note": "changed"},
    )
    assert response.status_code == 200, response.text
    response = post(
        client,
        write_headers,
        f"/import-reviews/{item['id']}/resolve",
        {"revision": 1, "record_revision": 1, "keep_merchant_unknown": True, "confirm": True},
    )
    assert response.status_code == 409
    assert (
        client.get(API + f"/records/{item['record_id']}", headers=READ).json()["state"] == "draft"
    )


def test_source_updates_append_only_and_block_unreviewed_confirmation(client, write_headers):
    item = commit_bill(client, write_headers, "source-update", "source-first").json()[
        "review_items"
    ][0]
    response = post(
        client,
        write_headers,
        "/imports/commit",
        {"source": {"format": "markdown", "content": eleme_bill("source-update", amount="31.50")}},
    )
    assert response.status_code == 201, response.text
    assert response.json()["created_count"] == 0
    with database.SessionLocal() as db:
        source = db.get(CaptureSource, UUID(item["source_id"]))
        assert source.raw_payload["rows"][0]["订单金额(元)"] == "25.00"
        observations = list(
            db.scalars(select(SourceObservation).where(SourceObservation.source_id == source.id))
        )
        assert {row.state for row in observations} == {"baseline", "pending"}
        pending = next(row for row in observations if row.state == "pending")
        observation_id = str(pending.id)
        assert pending.field_evidence[0]["locator"]["line"] == 3
    confirmed = client.post(
        API + f"/records/{item['record_id']}/confirm", headers={**write_headers, "If-Match": '"1"'}
    )
    assert confirmed.status_code == 409
    decision = post(
        client,
        write_headers,
        f"/source-observations/{observation_id}/decide",
        {
            "revision": 1,
            "action": "apply",
            "record_id": item["record_id"],
            "record_revision": 1,
            "reason": "核对原单金额",
        },
    )
    assert decision.status_code == 200, decision.text
    updated = client.get(API + f"/records/{item['record_id']}", headers=READ).json()
    assert updated["money_entry"]["amount"] == "31.5000"
    assert updated["state"] == "draft"
    assert updated["revision"] == 2
    assert (
        post(
            client,
            write_headers,
            f"/source-observations/{observation_id}/decide",
            {"revision": 1, "action": "keep", "reason": "重复点击"},
        ).status_code
        == 409
    )


def test_observation_revision_idempotency_and_no_auto_confirmation(client, write_headers):
    item = commit_bill(client, write_headers, "observation-api", "observation-api").json()[
        "review_items"
    ][0]
    payload = {
        "external_revision": "remote-2",
        "payload": {"rows": []},
        "parser": "test",
        "parser_version": "1",
    }
    path = f"/capture-sources/{item['source_id']}/observations"
    first = post(client, write_headers, path, payload)
    assert first.status_code == 201, first.text
    replay = post(client, write_headers, path, payload)
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["replayed"]
    assert (
        post(client, write_headers, path, {**payload, "payload": {"changed": True}}).status_code
        == 409
    )
    assert post(client, {**write_headers, "X-Dev-User": "bob"}, path, payload).status_code == 404
    assert (
        post(
            client,
            write_headers,
            path,
            {**payload, "external_revision": "3", "payload": {"access_token": "forbidden"}},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("extra", [False, True])
def test_forecast_price_basis_and_refund_exclusion(client, write_headers, extra):
    item = create_item(client, write_headers)
    for day in ["2026-06-01", "2026-07-01", "2026-08-01"]:
        record(client, write_headers, item=item, extra=extra, day=day, confirmed=True)
    record(client, write_headers, item=item, kind="refund", day="2026-08-02", confirmed=True)
    run = generate(client, write_headers, "2026-08-03")
    prediction = next(row for row in run["items"] if row["kind"] == "repeat_purchase")
    assert prediction["expected_amount"] == (None if extra else "30.0000")
    assert prediction["evidence"]["sample_count"] == 3
    assert prediction["evidence"]["amount_sample_count"] == (0 if extra else 3)
    assert prediction["confidence_kind"] == "heuristic_not_probability"
    with database.SessionLocal() as db:
        snapshot = build_input_snapshot(db, "alice", date(2026, 8, 3), "Asia/Shanghai", 90)
        assert len(snapshot["purchases"]) == 3


def test_feedback_survives_new_run_but_not_new_purchase(client, write_headers):
    item = create_item(client, write_headers)
    for day in ["2026-06-01", "2026-07-01", "2026-08-01"]:
        record(client, write_headers, item=item, day=day, confirmed=True)
    first = generate(client, write_headers, "2026-08-03")["items"][0]
    payload = {"revision": first["revision"], "feedback_revision": 0, "state": "dismissed"}
    response = post(client, write_headers, f"/forecast-items/{first['id']}/feedback", payload)
    assert response.status_code == 200, response.text
    second = generate(client, write_headers, "2026-08-04")["items"][0]
    assert second["state"] == "dismissed"
    assert second["episode_key"] == first["episode_key"]
    assert second["feedback_revision"] == 1
    assert (
        post(client, write_headers, f"/forecast-items/{second['id']}/feedback", payload).status_code
        == 409
    )
    record(client, write_headers, item=item, day="2026-08-05", confirmed=True)
    third = generate(client, write_headers, "2026-08-06")["items"][0]
    assert third["state"] == "active"
    assert third["episode_key"] != first["episode_key"]


def test_snooze_expiry_and_restore(client, write_headers):
    item = create_item(client, write_headers)
    for day in ["2026-06-01", "2026-07-01", "2026-08-01"]:
        record(client, write_headers, item=item, day=day, confirmed=True)
    prediction = generate(client, write_headers, "2026-08-03")["items"][0]
    path = f"/forecast-items/{prediction['id']}/feedback"
    payload = {
        "revision": 1,
        "feedback_revision": 0,
        "state": "snoozed",
        "snoozed_until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
    }
    assert post(client, write_headers, path, payload).status_code == 200
    with database.SessionLocal() as db:
        row = db.scalar(select(SuggestionFeedback))
        row.snoozed_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    restored = client.get(API + "/forecasts/latest", headers=READ).json()["run"]["items"][0]
    assert restored["state"] == "active"
    assert restored["feedback_revision"] == 1


def test_identity_assignment_merge_memory_and_idempotency(client, write_headers):
    source = create_item(client, write_headers, "滤芯原名")
    target = create_item(client, write_headers, "滤芯规范名")
    purchase = record(client, write_headers, item=source, confirmed=True)
    payload = {"target_id": target, "reason": "核对为相同规格"}
    path = f"/items/{source}/merge"
    first = post(client, write_headers, path, payload, "merge-once")
    assert first.status_code == 200, first.text
    assert post(client, write_headers, path, payload, "merge-once").status_code == 200
    assert (
        post(
            client,
            write_headers,
            f"/items/{target}/merge",
            {"target_id": source, "reason": "不可循环"},
        ).status_code
        == 409
    )
    memory = client.get(API + f"/identities/item/{target}/memory", headers=READ)
    assert memory.status_code == 200, memory.text
    assert memory.json()["count"] == 1
    assert memory.json()["items"][0]["consumption"]["lines"][0]["raw_name"] == "原始商品"
    assert (
        client.get(API + f"/records/{purchase['id']}", headers=READ).json()["money_entry"]["amount"]
        == "30.0000"
    )


def test_link_evidence_removes_only_explicit_duplicate_draft(client, write_headers):
    target = record(client, write_headers, confirmed=True)
    duplicate = commit_bill(client, write_headers, "duplicate-source", "duplicate-source").json()[
        "review_items"
    ][0]
    response = post(
        client,
        write_headers,
        f"/records/{target['id']}/link-evidence",
        {
            "source_id": duplicate["source_id"],
            "record_revision": target["revision"],
            "reason": "同一次消费的另一来源",
            "redundant_draft_id": duplicate["record_id"],
            "redundant_revision": 1,
        },
        "evidence-once",
    )
    assert response.status_code == 200, response.text
    assert client.get(API + f"/records/{duplicate['record_id']}", headers=READ).status_code == 404
    evidence = client.get(API + f"/records/{target['id']}/evidence", headers=READ)
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["sources"][0]["id"] == duplicate["source_id"]
    with database.SessionLocal() as db:
        assert len(list(db.scalars(select(LedgerRecord)))) == 1


def test_imported_draft_delete_keeps_source_and_cleans_review(client, write_headers):
    item = commit_bill(client, write_headers, "delete-import", "delete-import").json()[
        "review_items"
    ][0]
    response = client.delete(
        API + f"/records/{item['record_id']}", headers={**write_headers, "If-Match": '"1"'}
    )
    assert response.status_code == 204, response.text
    with database.SessionLocal() as db:
        assert db.get(CaptureSource, UUID(item["source_id"])) is not None
        assert db.get(ImportReviewItem, UUID(item["id"])) is None
    assert commit_bill(client, write_headers, "delete-import", "another-key").status_code == 409


PAYMENT_CSV = """微信支付账单明细列表
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号
2026-08-01 12:00:00,商户消费,餐厅,午餐,支出,￥31.25,零钱,支付成功,000123456789012345678901,shop-1
2026-08-02 12:00:00,退款,餐厅,午餐退款,收入,￥5.00,零钱,退款成功,000123456789012345678902,shop-1
2026-08-03 12:00:00,转账,朋友,/,支出,￥12.00,零钱,支付成功,000123456789012345678903,/
"""


def test_payment_csv_conservative_adapter_preserves_ids_and_partial_refunds(client, write_headers):
    parsed = parse_payment_import(PAYMENT_CSV, "csv")
    assert parsed.platform == "wechat"
    assert parsed.skipped_count == 1
    assert [row.record.money_entry.type for row in parsed.candidates] == ["expense", "refund"]
    assert parsed.candidates[0].raw_payload["rows"][0]["交易单号"].startswith("000")
    response = post(
        client,
        write_headers,
        "/imports/commit",
        {"source": {"format": "csv", "content": PAYMENT_CSV}},
    )
    assert response.status_code == 201, response.text
    assert response.json()["created_count"] == 2
    assert all(
        client.get(API + f"/records/{rid}", headers=READ).json()["state"] == "draft"
        for rid in response.json()["record_ids"]
    )


@pytest.mark.parametrize("bad", ["USD 31.25", "NaN", "0", "-12", "1e3"])
def test_payment_adapter_rejects_ambiguous_money(bad):
    with pytest.raises(ValueError, match="第 3 行"):
        parse_payment_import(PAYMENT_CSV.replace("￥31.25", bad), "csv")


def test_backtest_uses_only_pre_cutoff_samples_and_current_facts(client, write_headers):
    item = create_item(client, write_headers)
    for day in ["2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"]:
        record(client, write_headers, item=item, day=day, confirmed=True)
    result = client.get(API + "/forecast-evaluation?as_of=2026-08-03", headers=READ)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["sample_count"] == 3
    assert result.json()["observed_count"] == 1
    assert (
        client.get(
            API + "/forecast-evaluation?as_of=2026-08-03&timezone=Bad", headers=READ
        ).status_code
        == 422
    )


def test_old_forecast_snapshot_hash_still_verifies():
    from app.models import ForecastRun
    from app.services.forecast import _digest, calculate, verify_run

    snapshot = {
        "as_of": "2026-08-03",
        "timezone": "Asia/Shanghai",
        "horizon_days": 90,
        "commitments": [],
        "use_cycles": [],
        "purchases": [
            {
                "item_identity_id": "old-item",
                "item_name": "old",
                "record_id": str(index),
                "occurred_at": day + "T12:00:00+08:00",
                "amount": "10.0000",
                "currency": "CNY",
            }
            for index, day in enumerate(["2026-06-01", "2026-07-01", "2026-08-01"])
        ],
    }
    output = calculate(snapshot)
    assert "amount_basis" not in output[0]["evidence"]
    run = ForecastRun(
        algorithm_version="deterministic-v1", input_snapshot=snapshot, output_hash=_digest(output)
    )
    assert verify_run(run)


def test_alipay_adapter_accepts_preamble_bom_and_long_id():
    content = "\ufeff支付宝交易记录\n交易时间,交易分类,交易对方,商品说明,收/支,金额,交易状态,交易订单号,商家订单号\n2026-08-01 12:00:00,餐饮,餐厅,午餐,支出,12.50,交易成功,000123456789012345678901,shop-1\n"
    parsed = parse_payment_import(content, "csv")
    assert parsed.platform == "alipay"
    assert parsed.candidates[0].record.money_entry.payment_method == "alipay"
    assert parsed.candidates[0].record.money_entry.currency == "CNY"
    assert parsed.candidates[0].record.confirm is False


def test_identity_suggestion_rejection_does_not_reappear(client, write_headers):
    post(client, write_headers, "/merchants", {"canonical_name": "同名店"})
    post(client, write_headers, "/merchants", {"canonical_name": "同名 店"})
    response = post(client, write_headers, "/identities/propose")
    assert response.status_code == 200, response.text
    pending = client.get(API + "/workbench?kind=identity", headers=READ).json()["items"]
    assert len(pending) == 1
    assert (
        post(client, write_headers, f"/identity-suggestions/{pending[0]['id']}/reject").status_code
        == 200
    )
    post(client, write_headers, "/identities/propose")
    assert client.get(API + "/workbench?kind=identity", headers=READ).json()["items"] == []


def test_user_evidence_and_feedback_do_not_expand_service_scope(client, write_headers):
    from app.security import Actor, current_actor

    row = record(client, write_headers)
    client.app.dependency_overrides[current_actor] = lambda: Actor(
        owner_id="alice", actor_type="service"
    )
    try:
        assert client.get(API + "/workbench").status_code == 403
        assert client.get(API + f"/records/{row['id']}/evidence").status_code == 403
        assert post(client, write_headers, "/identities/propose").status_code == 403
    finally:
        client.app.dependency_overrides.pop(current_actor)


def test_identity_edit_and_alias_require_idempotency(client, write_headers):
    merchant = post(client, write_headers, "/merchants", {"canonical_name": "旧名"}).json()
    path = API + f"/merchants/{merchant['id']}"
    assert (
        client.patch(path, headers=write_headers, json={"canonical_name": "新名"}).status_code
        == 400
    )
    headers = {**write_headers, "Idempotency-Key": "edit-identity"}
    for _ in range(2):
        assert (
            client.patch(path, headers=headers, json={"canonical_name": "新名"}).status_code == 200
        )
    assert client.patch(path, headers=headers, json={"canonical_name": "另一名"}).status_code == 409
    alias = post(
        client,
        write_headers,
        f"/merchants/{merchant['id']}/aliases",
        {"alias": "门店原文"},
        "alias-once",
    )
    assert alias.status_code == 201
    replay = post(
        client,
        write_headers,
        f"/merchants/{merchant['id']}/aliases",
        {"alias": "门店原文"},
        "alias-once",
    )
    assert replay.json()["id"] == alias.json()["id"]


def test_intent_draft_retry_does_not_recompute_timestamp(client, write_headers):
    intent = post(
        client, write_headers, "/intents", {"intent_type": "buy", "title": "计划测试"}
    ).json()
    path = f"/intents/{intent['id']}/draft"
    first = post(client, write_headers, path, key="intent-draft-once")
    second = post(client, write_headers, path, key="intent-draft-once")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["state"] == "draft"


def test_manifest_and_private_response_cache_policy(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["share_target"]["method"] == "GET"
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/static/core.js").headers["cache-control"] == "no-cache"


def test_malformed_csv_is_a_validation_error(client, write_headers):
    malformed = PAYMENT_CSV.replace("￥31.25", '"unclosed')
    response = post(
        client, write_headers, "/imports/preview", {"format": "csv", "content": malformed}
    )
    assert response.status_code == 422, response.text
