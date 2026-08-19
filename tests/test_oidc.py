from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from starlette.requests import Request

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.oidc import _claims, exact_redirect_uri


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def request_for(url: str) -> Request:
    parsed = urllib.parse.urlsplit(url)
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": parsed.scheme,
            "server": (parsed.hostname, parsed.port or 443),
            "path": parsed.path,
            "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(),
            "headers": [(b"host", parsed.netloc.encode())],
        }
    )


def oidc_settings() -> Settings:
    return Settings(
        env="test",
        database_url="sqlite+pysqlite:///:memory:",
        oidc_issuer="https://identity.example.invalid",
        oidc_client_id="shadow-ledger",
        oidc_client_secret="oidc-secret",
        oidc_callbacks=[
            "https://ledger.example.invalid/auth/callback",
            "https://ledger-lan.example.invalid/auth/callback",
        ],
        allowed_origins=[
            "https://ledger.example.invalid",
            "https://ledger-lan.example.invalid",
        ],
        session_secret="test-session-secret-with-enough-entropy",
        dev_auth=False,
    )


def test_callback_allowlist_is_exact():
    settings = oidc_settings()
    assert (
        exact_redirect_uri(request_for("https://ledger.example.invalid/login"), settings)
        == settings.oidc_callbacks[0]
    )
    try:
        exact_redirect_uri(request_for("https://evil.example/login"), settings)
    except Exception as exc:
        assert getattr(exc, "code", None) == "untrusted_callback"
    else:
        raise AssertionError("untrusted host was accepted")


def test_id_token_validates_signature_issuer_audience_nonce_fields(monkeypatch):
    settings = oidc_settings()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "key-1", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(
        "app.oidc.httpx.get", lambda *_args, **_kwargs: FakeResponse({"keys": [jwk]})
    )
    now = datetime.now(UTC)
    claims = {
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_client_id,
        "sub": "user-1",
        "nonce": "nonce-1",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "groups": ["ledger-users"],
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})
    decoded = _claims(token, {"jwks_uri": "https://identity.example.invalid/jwks"}, settings)
    assert decoded["sub"] == "user-1"

    wrong_audience = jwt.encode(
        {**claims, "aud": "another-client"},
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    try:
        _claims(
            wrong_audience,
            {"jwks_uri": "https://identity.example.invalid/jwks"},
            settings,
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "invalid_id_token"
    else:
        raise AssertionError("wrong audience was accepted")


def test_full_oidc_flow_consumes_state_and_creates_opaque_session(monkeypatch):
    settings = oidc_settings()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "key-1", "alg": "RS256", "use": "sig"})
    metadata = {
        "issuer": settings.oidc_issuer,
        "authorization_endpoint": "https://identity.example.invalid/authorize",
        "token_endpoint": "https://identity.example.invalid/token",
        "jwks_uri": "https://identity.example.invalid/jwks",
    }
    monkeypatch.setattr("app.oidc.discovery", lambda _settings: metadata)
    app = create_app(settings, "sqlite+pysqlite:///:memory:")
    with TestClient(app, base_url="https://ledger.example.invalid") as client:
        from app import db

        Base.metadata.create_all(db.engine)
        login = client.get("/login?return_to=/planning", follow_redirects=False)
        assert login.status_code == 302
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(login.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [settings.oidc_callbacks[0]]
        nonce = query["nonce"][0]
        state = query["state"][0]
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "iss": settings.oidc_issuer,
                "aud": settings.oidc_client_id,
                "sub": "user-1",
                "nonce": nonce,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "groups": ["ledger-users"],
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "key-1"},
        )
        monkeypatch.setattr(
            "app.oidc.httpx.post", lambda *_args, **_kwargs: FakeResponse({"id_token": token})
        )
        monkeypatch.setattr(
            "app.oidc.httpx.get", lambda *_args, **_kwargs: FakeResponse({"keys": [jwk]})
        )
        callback = client.get(f"/auth/callback?code=code-1&state={state}", follow_redirects=False)
        assert callback.status_code == 303, callback.text
        assert callback.headers["location"] == "/planning"
        assert "__Host-ledger-session=" in callback.headers["set-cookie"]
        replay = client.get(f"/auth/callback?code=code-1&state={state}", follow_redirects=False)
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "login_binding_missing"
