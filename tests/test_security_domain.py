from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.config import Settings
from app.db import Base, make_engine
from app.models import LedgerRecord, MoneyEntry
from app.oidc import pkce_challenge, safe_return_to
from app.security import validate_csrf


def test_production_configuration_fails_closed():
    with pytest.raises(ValidationError):
        Settings(env="production", oidc_issuer="http://identity.invalid")


def test_return_to_and_pkce_are_strict():
    assert safe_return_to("//evil.example") == "/"
    assert safe_return_to("https://evil.example") == "/"
    assert safe_return_to("/records?state=draft") == "/records?state=draft"
    assert pkce_challenge("verifier") == "iMnq5o6zALKXGivsnlom_0F5_WYda32GHkxlV7mq7hQ"


def test_cookie_writes_require_exact_origin_and_csrf():
    settings = Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        oidc_client_secret="oidc-secret",
        session_secret="session-secret",
        allowed_origins=["https://ledger.example.invalid"],
        dev_auth=False,
    )
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("ledger.example.invalid", 443),
        "path": "/api/v1/records",
        "raw_path": b"/api/v1/records",
        "query_string": b"",
        "headers": [(b"origin", b"https://evil.example.invalid")],
    }
    with pytest.raises(Exception) as caught:
        validate_csrf(Request(scope), settings)
    assert getattr(caught.value, "code", None) == "invalid_origin"


def test_database_rejects_negative_amount():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        record = LedgerRecord(
            owner_id="alice",
            record_kind="money_only",
            state="draft",
            occurred_at=datetime(2026, 8, 19, tzinfo=UTC),
            timezone="Asia/Shanghai",
        )
        db.add(record)
        db.flush()
        db.add(
            MoneyEntry(
                record_id=record.id,
                type="expense",
                amount=Decimal("-1"),
                currency="CNY",
                title="invalid",
            )
        )
        with pytest.raises((IntegrityError, TypeError)):
            db.commit()
