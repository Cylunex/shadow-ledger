from __future__ import annotations

import os

os.environ.update(
    {
        "LEDGER_ENV": "test",
        "LEDGER_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "LEDGER_OIDC_CLIENT_SECRET": "test-oidc-secret",
        "LEDGER_SESSION_SECRET": "test-session-secret-with-enough-entropy",
        "LEDGER_DEV_AUTH": "true",
    }
)

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base
from app.main import create_app


def test_database_url():
    url = os.environ.get("LEDGER_TEST_POSTGRES_URL", "sqlite+pysqlite:///:memory:")
    if url.startswith("postgresql"):
        from sqlalchemy.engine import make_url

        if not (make_url(url).database or "").startswith("ledger_test_"):
            raise RuntimeError("PostgreSQL tests require an isolated ledger_test_* database")
    return url


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        database_url=test_database_url(),
        oidc_issuer="https://identity.example.invalid",
        oidc_client_id="shadow-ledger",
        oidc_client_secret="test-oidc-secret",
        oidc_callbacks=["https://testserver/auth/callback"],
        allowed_origins=["https://testserver"],
        session_secret="test-session-secret-with-enough-entropy",
        dev_auth=True,
    )


@pytest.fixture
def client(settings: Settings):
    url = test_database_url()
    app = create_app(settings, url)
    with TestClient(app, base_url="https://testserver") as test_client:
        from app import db

        assert db.engine is not None
        Base.metadata.create_all(db.engine)
        test_client.cookies.set("__Host-ledger-csrf", "test-csrf", domain="testserver", path="/")
        yield test_client
        Base.metadata.drop_all(db.engine)


@pytest.fixture
def write_headers() -> dict[str, str]:
    return {
        "Origin": "https://testserver",
        "X-CSRF-Token": "test-csrf",
        "X-Dev-User": "alice",
        "Cookie": "__Host-ledger-csrf=test-csrf",
    }


@pytest.fixture
def idempotent_headers(write_headers: dict[str, str]) -> dict[str, str]:
    return {**write_headers, "Idempotency-Key": "test-key-1"}
