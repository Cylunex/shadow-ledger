from __future__ import annotations

from datetime import UTC, datetime, timedelta


def test_text_capture_preserves_source_and_creates_draft(client, write_headers):
    response = client.post(
        "/api/v1/capture/text",
        json={"text": "昨天外卖黄焖鸡，金额 27.5，味道一般", "external_id": "msg-1"},
        headers={**write_headers, "Idempotency-Key": "capture-1"},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["state"] == "draft"
    assert record["money_entry"]["amount"] == "27.5000"
    assert record["consumption"]["scene"] == "delivery"
    source = client.get(
        f"/api/v1/capture-sources/{record['sources'][0]['source_id']}",
        headers={"X-Dev-User": "alice"},
    )
    assert source.json()["capture_state"] == "parsed"


def test_intent_requires_confirmed_record_for_completion(client, write_headers):
    intent = client.post(
        "/api/v1/intents",
        json={
            "intent_type": "buy",
            "title": "新键盘",
            "expected_amount": "399.00",
            "currency": "CNY",
            "priority": "normal",
            "state": "planned",
        },
        headers={**write_headers, "Idempotency-Key": "intent-1"},
    ).json()
    draft = client.post(
        f"/api/v1/intents/{intent['id']}/draft",
        headers={**write_headers, "Idempotency-Key": "intent-draft-1"},
    ).json()
    failed = client.post(
        f"/api/v1/intents/{intent['id']}/complete?record_id={draft['id']}",
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert failed.status_code == 422
    client.post(
        f"/api/v1/records/{draft['id']}/confirm",
        headers={**write_headers, "If-Match": '"1"'},
    )
    completed = client.post(
        f"/api/v1/intents/{intent['id']}/complete?record_id={draft['id']}",
        headers={**write_headers, "If-Match": '"1"'},
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"


def test_recurring_commitment_only_creates_draft_and_is_idempotent(client, write_headers):
    due = datetime.now(UTC) + timedelta(days=2)
    commitment = client.post(
        "/api/v1/recurring-commitments",
        json={
            "kind": "subscription",
            "title": "云服务",
            "expected_amount": "12.00",
            "currency": "CNY",
            "recurrence_rule": "FREQ=MONTHLY",
            "timezone": "Asia/Shanghai",
            "next_due_at": due.isoformat(),
            "remind_before_seconds": 259200,
        },
        headers={**write_headers, "Idempotency-Key": "commitment-1"},
    ).json()
    first = client.post(
        f"/api/v1/recurring-commitments/{commitment['id']}/draft",
        headers={**write_headers, "Idempotency-Key": "commitment-draft-1"},
    )
    second = client.post(
        f"/api/v1/recurring-commitments/{commitment['id']}/draft",
        headers={**write_headers, "Idempotency-Key": "commitment-draft-1"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["state"] == "draft"
    assert second.json()["id"] == first.json()["id"]


def test_budget_progress_uses_net_spending(client, write_headers):
    categories = client.get("/api/v1/categories", headers={"X-Dev-User": "alice"}).json()["items"]
    food = next(row for row in categories if row["key"] == "food")
    month = datetime.now(UTC).date().replace(day=1).isoformat()
    budget = client.post(
        "/api/v1/budget-targets",
        json={
            "category_id": food["id"],
            "budget_month": month,
            "monthly_amount": "500",
            "currency": "CNY",
        },
        headers={**write_headers, "Idempotency-Key": "budget-1"},
    )
    assert budget.status_code == 201, budget.text
    progress = client.get(
        f"/api/v1/insights/budgets?month={month[:7]}", headers={"X-Dev-User": "alice"}
    ).json()
    assert progress["items"][0]["remaining"] == "500.0000"
