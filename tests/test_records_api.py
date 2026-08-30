from __future__ import annotations

from datetime import UTC, datetime


def money_payload(amount="27.5000", confirm=False):
    return {
        "occurred_at": datetime.now(UTC).isoformat(),
        "timezone": "Asia/Shanghai",
        "note": "",
        "money_entry": {
            "type": "expense",
            "amount": amount,
            "currency": "CNY",
            "category_key": None,
            "title": "午餐",
        },
        "consumption": None,
        "confirm": confirm,
    }


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_draft_review_actions_are_present_in_browser_ui(client):
    page = client.get("/")
    script = client.get("/static/app.js?v=20260830-import-review")

    assert page.status_code == script.status_code == 200
    assert 'id="batch-confirm"' in page.text
    assert 'id="record-filter-form"' in page.text
    assert 'id="filter-query"' in page.text
    assert 'id="filter-state"' in page.text
    assert 'id="load-more"' in page.text
    assert "app.css?v=20260830-import-review" in page.text
    assert "app.js?v=20260830-import-review" in page.text
    assert "编辑草稿" in script.text
    assert "确认入账" in script.text
    assert "删除草稿" in script.text
    assert "撤销记录" in script.text
    assert "全部确认入账" in script.text
    assert "/records/batch-confirm" in script.text
    assert "/void" in script.text
    assert "amount_min" in script.text
    assert "导入复核工作台" in script.text
    assert "/import-reviews" in script.text
    assert "occurred_from" in script.text


def test_empty_insight_endpoints_are_stable(client):
    headers = {"X-Dev-User": "alice"}
    for path in ("/insights/scenes", "/insights/merchants", "/insights/items"):
        response = client.get(f"/api/v1{path}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []


def test_money_record_lifecycle_and_decimal(client, idempotent_headers, write_headers):
    created = client.post("/api/v1/records", json=money_payload(), headers=idempotent_headers)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["money_entry"]["amount"] == "27.5000"
    assert record["state"] == "draft"
    assert created.headers["etag"] == '"1"'

    confirmed = client.post(
        f"/api/v1/records/{record['id']}/confirm",
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "confirmed"
    assert confirmed.headers["etag"] == '"2"'

    conflict = client.post(
        f"/api/v1/records/{record['id']}/void",
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision_conflict"

    voided = client.post(
        f"/api/v1/records/{record['id']}/void",
        headers={**write_headers, "If-Match": '"2"'},
    )
    assert voided.status_code == 200
    assert voided.json()["state"] == "voided"

    summary = client.get("/api/v1/insights/summary", headers={"X-Dev-User": "alice"})
    assert summary.json()["expense"] == "0.0000"


def test_idempotency_replay_and_payload_mismatch(client, idempotent_headers):
    payload = money_payload()
    first = client.post("/api/v1/records", json=payload, headers=idempotent_headers)
    second = client.post("/api/v1/records", json=payload, headers=idempotent_headers)
    assert first.json()["id"] == second.json()["id"]
    changed = client.post(
        "/api/v1/records", json=money_payload("28.00"), headers=idempotent_headers
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_mismatch"


def test_batch_confirm_is_atomic_and_revision_checked(client, write_headers):
    drafts = [
        client.post(
            "/api/v1/records",
            json=money_payload(str(amount)),
            headers={**write_headers, "Idempotency-Key": f"batch-draft-{amount}"},
        ).json()
        for amount in (31, 32)
    ]
    confirmed = client.post(
        "/api/v1/records/batch-confirm",
        json={"records": [{"id": row["id"], "revision": row["revision"]} for row in drafts]},
        headers=write_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed_count"] == 2
    for row in drafts:
        current = client.get(f"/api/v1/records/{row['id']}", headers={"X-Dev-User": "alice"}).json()
        assert current["state"] == "confirmed"
        assert current["revision"] == 2

    conflicted_drafts = [
        client.post(
            "/api/v1/records",
            json=money_payload(str(amount)),
            headers={**write_headers, "Idempotency-Key": f"conflict-draft-{amount}"},
        ).json()
        for amount in (41, 42)
    ]
    conflict = client.post(
        "/api/v1/records/batch-confirm",
        json={
            "records": [
                {"id": conflicted_drafts[0]["id"], "revision": 1},
                {"id": conflicted_drafts[1]["id"], "revision": 99},
            ]
        },
        headers=write_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "revision_conflict"
    for row in conflicted_drafts:
        current = client.get(f"/api/v1/records/{row['id']}", headers={"X-Dev-User": "alice"}).json()
        assert current["state"] == "draft"
        assert current["revision"] == 1


def test_draft_can_be_edited_with_revision_check(client, write_headers):
    created = client.post(
        "/api/v1/records",
        json=money_payload(),
        headers={**write_headers, "Idempotency-Key": "editable-draft"},
    ).json()
    payload = money_payload("35.25")
    payload.pop("confirm")
    updated = client.patch(
        f"/api/v1/records/{created['id']}",
        json=payload,
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["money_entry"]["amount"] == "35.2500"

    stale = client.patch(
        f"/api/v1/records/{created['id']}",
        json=payload,
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"


def test_record_filters_and_cursor_pagination(client, write_headers):
    occurred_at = "2026-08-20T12:00:00+08:00"
    records = []
    for index, (entry_type, title, amount) in enumerate(
        (
            ("expense", "筛选午餐", "28.00"),
            ("income", "筛选工资", "8000.00"),
            ("refund", "筛选退款", "12.00"),
        )
    ):
        payload = money_payload(amount, confirm=True)
        payload["occurred_at"] = occurred_at
        payload["money_entry"]["type"] = entry_type
        payload["money_entry"]["title"] = title
        records.append(
            client.post(
                "/api/v1/records",
                json=payload,
                headers={**write_headers, "Idempotency-Key": f"filter-{index}"},
            ).json()
        )

    filtered = client.get(
        "/api/v1/records?money_type=income&query=工资&occurred_from=2026-08-20T00:00:00%2B08:00&occurred_to=2026-08-21T00:00:00%2B08:00",
        headers={"X-Dev-User": "alice"},
    )
    assert filtered.status_code == 200, filtered.text
    assert [row["id"] for row in filtered.json()["items"]] == [records[1]["id"]]

    seen = []
    cursor = None
    while True:
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/v1/records", params=params, headers={"X-Dev-User": "alice"})
        assert page.status_code == 200, page.text
        seen.extend(row["id"] for row in page.json()["items"])
        cursor = page.json()["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 3
    assert set(seen) == {record["id"] for record in records}


def test_unknown_amount_consumption_can_be_confirmed_then_completed(client, write_headers):
    payload = {
        "occurred_at": datetime.now(UTC).isoformat(),
        "timezone": "Asia/Shanghai",
        "money_entry": None,
        "consumption": {
            "scene": "delivery",
            "merchant_name_raw": "原始商家",
            "channel_name_raw": "原始渠道",
            "lines": [{"raw_name": "原始菜名", "sort_order": 0}],
        },
        "confirm": False,
    }
    created = client.post(
        "/api/v1/records",
        json=payload,
        headers={**write_headers, "Idempotency-Key": "unknown-1"},
    ).json()
    confirmed = client.post(
        f"/api/v1/records/{created['id']}/confirm",
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert confirmed.json()["money_entry"] is None
    added = client.post(
        f"/api/v1/records/{created['id']}/money-entry",
        json={"type": "expense", "amount": "19.99", "currency": "CNY", "title": "外卖"},
        headers={**write_headers, "If-Match": '"2"'},
    )
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["money_entry"]["amount"] == "19.9900"
    assert body["consumption"]["merchant_name_raw"] == "原始商家"


def test_refund_is_separate_fact(client, write_headers):
    expense = client.post(
        "/api/v1/records",
        json=money_payload("100.00", confirm=True),
        headers={**write_headers, "Idempotency-Key": "expense-1"},
    ).json()
    refund_payload = money_payload("20.00", confirm=True)
    refund_payload["money_entry"]["type"] = "refund"
    refund_payload["money_entry"]["related_entry_id"] = expense["money_entry"]["id"]
    refund = client.post(
        "/api/v1/records",
        json=refund_payload,
        headers={**write_headers, "Idempotency-Key": "refund-1"},
    )
    assert refund.status_code == 201, refund.text
    assert refund.json()["id"] != expense["id"]
    summary = client.get("/api/v1/insights/summary", headers={"X-Dev-User": "alice"}).json()
    assert summary["expense"] == "100.0000"
    assert summary["refund"] == "20.0000"
    assert summary["net_spending"] == "80.0000"
    filtered = client.get(
        "/api/v1/records?money_type=expense&amount_min=90&amount_max=110&query=午餐",
        headers={"X-Dev-User": "alice"},
    )
    assert filtered.status_code == 200, filtered.text
    assert [row["id"] for row in filtered.json()["items"]] == [expense["id"]]


def test_owner_isolation_and_draft_delete(client, write_headers):
    created = client.post(
        "/api/v1/records",
        json=money_payload(),
        headers={**write_headers, "Idempotency-Key": "owner-1"},
    ).json()
    assert (
        client.get(f"/api/v1/records/{created['id']}", headers={"X-Dev-User": "bob"}).status_code
        == 404
    )
    deleted = client.delete(
        f"/api/v1/records/{created['id']}",
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert deleted.status_code == 204
