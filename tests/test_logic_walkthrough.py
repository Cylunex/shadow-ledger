"""Cross-feature regressions from the three-pass workflow audit."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app import db as database
from app.models import RecurringCommitment
from app.security import Actor, current_actor
from app.worker import create_due_reminders


def post(client, headers, path, body):
    return client.post(path, json=body, headers={**headers, "Idempotency-Key": str(uuid4())})


def record(client, headers, amount="10", currency="CNY", kind="expense", **extra):
    body = {
        "occurred_at": "2026-09-01T00:30:00+08:00",
        "confirm": True,
        "money_entry": {"type": kind, "amount": amount, "currency": currency},
        **extra,
    }
    response = post(client, headers, "/api/v1/records", body)
    assert response.status_code == 201, response.text
    return response.json()


def commitment(client, headers, **extra):
    body = {
        "kind": "subscription",
        "title": "合成周期",
        "expected_amount": "12",
        "currency": "CNY",
        "recurrence_rule": "FREQ=MONTHLY",
        "timezone": "Asia/Shanghai",
        "next_due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        **extra,
    }
    response = post(client, headers, "/api/v1/recurring-commitments", body)
    assert response.status_code == 201, response.text
    return response.json(), body


def test_calendar_month_agrees_between_browser_agent_and_budget(client, write_headers):
    record(client, write_headers, amount="12")
    record(client, write_headers, amount="99", occurred_at="2026-09-30T16:30:00Z")
    post(
        client,
        write_headers,
        "/api/v1/budget-targets",
        {"budget_month": "2026-09-01", "monthly_amount": "100", "currency": "CNY"},
    )
    summary = client.get("/api/v1/insights/summary?month=2026-09", headers=write_headers).json()
    assert Decimal(summary["expense"]) == 12
    budget = client.get("/api/v1/insights/budgets?month=2026-09", headers=write_headers).json()
    assert Decimal(budget["items"][0]["net_spending"]) == 12
    catalog = client.get("/api/v1/agent/catalog", headers=write_headers).json()
    query = client.post(
        "/api/v1/agent/tools/call",
        headers=write_headers,
        json={
            "skill": "research",
            "catalog_hash": catalog["catalog_hash"],
            "tool": "ledger_overview",
            "arguments": {"month": "2026-09", "timezone": "Asia/Shanghai"},
        },
    )
    assert query.status_code == 200, query.text
    assert Decimal(query.json()["metrics"][0]["value"]) == 12


def test_merchant_totals_never_mix_currency_or_treat_refund_as_purchase(client, write_headers):
    consumption = {"scene": "drink", "merchant_name_raw": "合成商家"}
    for amount, currency, kind in [
        ("20", "CNY", "expense"),
        ("5", "CNY", "refund"),
        ("3", "USD", "expense"),
        ("40", "CNY", "income"),
    ]:
        record(client, write_headers, amount, currency, kind, consumption=consumption)
    result = client.get("/api/v1/insights/merchants?currency=CNY", headers=write_headers).json()
    assert result["currency"] == "CNY"
    assert Decimal(result["items"][0]["amount"]) == 15
    assert result["items"][0]["count"] == 1


def test_manual_recurring_draft_does_not_stall_schedule_or_retry_next_occurrence(
    client, write_headers
):
    row, _ = commitment(client, write_headers)
    headers = {**write_headers, "Idempotency-Key": "stable-occurrence-request"}
    path = f"/api/v1/recurring-commitments/{row['id']}/draft"
    first = client.post(path, headers=headers)
    assert first.status_code == 201
    with database.SessionLocal() as db:
        create_due_reminders(db)
        updated = db.get(RecurringCommitment, UUID(row["id"]))
        assert updated.next_due_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(days=10)
        assert updated.revision > row["revision"]
    retry = client.post(path, headers=headers)
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] == first.json()["id"]


def test_recurring_reminder_preserves_local_clock_across_dst(client, write_headers):
    row, _ = commitment(
        client,
        write_headers,
        timezone="America/New_York",
        recurrence_rule="FREQ=DAILY",
        next_due_at="2026-03-07T09:00:00-05:00",
    )
    with database.SessionLocal() as db:
        create_due_reminders(db)
        updated = db.get(RecurringCommitment, UUID(row["id"]))
        assert updated.next_due_at.replace(tzinfo=UTC) == datetime(2026, 3, 8, 13, tzinfo=UTC)


def test_paused_commitment_cannot_create_new_draft(client, write_headers):
    row, body = commitment(client, write_headers)
    client.patch(
        f"/api/v1/recurring-commitments/{row['id']}?state=paused",
        json=body,
        headers={**write_headers, "If-Match": str(row["revision"])},
    )
    response = client.post(
        f"/api/v1/recurring-commitments/{row['id']}/draft",
        headers={**write_headers, "Idempotency-Key": "paused-draft"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "path,body",
    [
        ("/intents", {"intent_type": "buy", "title": "合成计划"}),
        (
            "/recurring-commitments",
            {
                "kind": "subscription",
                "title": "合成周期",
                "recurrence_rule": "FREQ=MONTHLY",
                "next_due_at": "2026-10-01T10:00:00Z",
            },
        ),
    ],
)
def test_planning_foreign_identity_is_rejected(client, write_headers, path, body):
    other = post(
        client,
        {**write_headers, "X-Dev-User": "bob"},
        "/api/v1/merchants",
        {"canonical_name": "别人的商家", "merchant_type": "other"},
    ).json()
    response = post(client, write_headers, "/api/v1" + path, {**body, "merchant_id": other["id"]})
    assert response.status_code == 422


def test_completed_intent_cannot_be_reopened_by_patch_or_generate_draft(client, write_headers):
    body = {"intent_type": "buy", "title": "完成的计划"}
    intent = post(client, write_headers, "/api/v1/intents", body).json()
    fact = record(client, write_headers)
    result = client.post(
        f"/api/v1/intents/{intent['id']}/complete?record_id={fact['id']}",
        headers={**write_headers, "If-Match": "1"},
    )
    assert result.status_code == 200
    response = client.patch(
        f"/api/v1/intents/{intent['id']}", json=body, headers={**write_headers, "If-Match": "2"}
    )
    assert response.status_code == 409
    response = client.post(
        f"/api/v1/intents/{intent['id']}/draft",
        headers={**write_headers, "Idempotency-Key": "completed-draft"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/v1/exports/00000000-0000-0000-0000-000000000001", None),
        ("POST", "/api/v1/exports", None),
        ("GET", "/api/v1/intents", None),
        ("POST", "/api/v1/intents", {"intent_type": "buy", "title": "越权计划"}),
        ("GET", "/api/v1/insights/summary", None),
    ],
)
def test_service_without_scope_cannot_use_browser_endpoints(
    client, write_headers, method, path, body
):
    client.app.dependency_overrides[current_actor] = lambda: Actor("alice", "service", frozenset())
    try:
        response = client.request(
            method, path, json=body, headers={**write_headers, "Idempotency-Key": "deny"}
        )
        assert response.status_code == 403, response.text
    finally:
        client.app.dependency_overrides.pop(current_actor)


def test_draft_only_service_cannot_modify_confirmed_fact(client, write_headers):
    fact = record(client, write_headers)
    client.app.dependency_overrides[current_actor] = lambda: Actor(
        "alice", "service", frozenset({"ledger.write-draft"})
    )
    try:
        response = client.patch(
            f"/api/v1/records/{fact['id']}",
            headers={**write_headers, "If-Match": str(fact["revision"])},
            json={
                "money_entry": {"type": "expense", "amount": "999", "currency": "CNY"},
                "correction_reason": "不能借草稿权限改事实",
            },
        )
        assert response.status_code == 403
    finally:
        client.app.dependency_overrides.pop(current_actor)


@pytest.mark.parametrize(
    "extra",
    [
        {"occurred_at": "2026-09-01T00:00:00"},
        {"timezone": "bad/zone"},
        {"money_entry": {"amount": 0.1, "type": "expense", "currency": "CNY"}},
    ],
)
def test_record_rejects_ambiguous_time_and_binary_float(client, write_headers, extra):
    response = post(
        client,
        write_headers,
        "/api/v1/records",
        {
            "occurred_at": "2026-09-01T00:00:00Z",
            "money_entry": {"type": "expense", "amount": "10", "currency": "CNY"},
            **extra,
        },
    )
    assert response.status_code == 422


def test_lan_bypass_requires_configured_host_and_checks_origin_after_prefix(client, settings):
    from app.models import LocalIdentity

    settings.dev_auth = False
    with database.SessionLocal.begin() as db:
        db.add(LocalIdentity(issuer="https://identity.example.invalid", subject="synthetic-lan"))
    headers = {"Host": "127.0.0.1", "X-Shadow-Lan-Bypass": "1", "X-Forwarded-Prefix": "/ledger"}
    assert client.get("/api/v1/me", headers=headers).status_code == 401
    settings.lan_bypass_hosts = ["127.0.0.1"]
    body = {"intent_type": "buy", "title": "合成局域网请求"}
    response = post(client, {**headers, "Origin": "https://127.0.0.1"}, "/api/v1/intents", body)
    assert response.status_code == 201, response.text
    denied = post(
        client, {**headers, "Origin": "https://other.example.invalid"}, "/api/v1/intents", body
    )
    assert denied.status_code == 403


def test_oidc_rejects_wrong_authorized_party_and_shared_secret_algorithm(monkeypatch):
    import json

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    from test_oidc import FakeResponse, oidc_settings

    from app.errors import AppError
    from app.oidc import _claims

    settings = oidc_settings()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="audit-key", alg="RS256", use="sig")
    monkeypatch.setattr("app.oidc.httpx.get", lambda *args, **kwargs: FakeResponse({"keys": [jwk]}))
    claims = {
        "iss": settings.oidc_issuer,
        "aud": [settings.oidc_client_id, "other"],
        "sub": "test",
        "nonce": "test",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    for azp in (None, "other"):
        token = jwt.encode(
            {**claims, **({"azp": azp} if azp else {})},
            key,
            algorithm="RS256",
            headers={"kid": "audit-key"},
        )
        with pytest.raises(AppError, match="invalid_id_token"):
            _claims(token, {"jwks_uri": "https://identity.example.invalid/jwks"}, settings)
    token = jwt.encode(
        {**claims, "azp": settings.oidc_client_id},
        key,
        algorithm="RS256",
        headers={"kid": "audit-key"},
    )
    assert (
        _claims(token, {"jwks_uri": "https://identity.example.invalid/jwks"}, settings)["sub"]
        == "test"
    )
    token = jwt.encode(
        claims,
        "synthetic-secret-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": "audit-key"},
    )
    with pytest.raises(AppError, match="invalid_id_token"):
        _claims(token, {"jwks_uri": "https://identity.example.invalid/jwks"}, settings)
