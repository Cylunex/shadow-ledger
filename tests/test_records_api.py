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
