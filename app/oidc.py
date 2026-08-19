from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.models import BrowserSession, LocalIdentity, OidcTransaction
from app.security import CSRF_COOKIE, SESSION_COOKIE, digest, new_csrf_token

router = APIRouter()
BINDING_COOKIE = "__Host-ledger-login"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


def _fernet(settings: Settings) -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.resolved_session_secret.encode()).digest()
    )
    return Fernet(key)


def safe_return_to(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value[:1000]


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def exact_redirect_uri(request: Request, settings: Settings) -> str:
    current_origin = f"{request.url.scheme}://{request.url.netloc}"
    candidate = f"{current_origin}/auth/callback"
    if candidate not in settings.oidc_callbacks:
        raise AppError(400, "untrusted_callback", "当前入口未注册登录回调")
    return candidate


def discovery(settings: Settings) -> dict:
    url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError(502, "oidc_unavailable", "身份服务暂不可用") from exc
    if payload.get("issuer") != settings.oidc_issuer:
        raise AppError(502, "oidc_issuer_mismatch", "身份服务 issuer 不匹配")
    return payload


@router.get("/login")
def login(
    request: Request,
    return_to: str | None = None,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    metadata = discovery(settings)
    redirect_uri = exact_redirect_uri(request, settings)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    binding = secrets.token_urlsafe(32)
    transaction = OidcTransaction(
        state_hash=digest(state),
        browser_binding_hash=digest(binding),
        nonce_hash=digest(nonce),
        pkce_verifier_ciphertext=_fernet(settings).encrypt(verifier.encode()),
        redirect_uri=redirect_uri,
        return_to=safe_return_to(return_to),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(transaction)
    db.commit()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email groups",
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        f"{metadata['authorization_endpoint']}?{urllib.parse.urlencode(params)}", status_code=302
    )
    response.set_cookie(
        BINDING_COOKIE, binding, secure=True, httponly=True, samesite="lax", path="/", max_age=600
    )
    return response


def _claims(id_token: str, metadata: dict, settings: Settings) -> dict:
    try:
        jwks_response = httpx.get(metadata["jwks_uri"], timeout=5.0)
        jwks_response.raise_for_status()
        key_set = jwt.PyJWKSet.from_dict(jwks_response.json())
        header = jwt.get_unverified_header(id_token)
        keys = [key.key for key in key_set.keys if key.key_id == header.get("kid")]
        if len(keys) != 1:
            raise AppError(401, "oidc_unknown_key", "ID Token 签名密钥无效")
        return jwt.decode(
            id_token,
            keys[0],
            algorithms=[header.get("alg")],
            audience=settings.oidc_client_id,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
    except AppError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWTError) as exc:
        raise AppError(401, "invalid_id_token", "ID Token 校验失败") from exc


@router.get("/auth/callback")
def callback(
    request: Request,
    code: str,
    state: str,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    binding = request.cookies.get(BINDING_COOKIE)
    if not binding:
        raise AppError(401, "login_binding_missing", "登录浏览器绑定已失效")
    transaction = db.scalar(
        select(OidcTransaction).where(OidcTransaction.state_hash == digest(state)).with_for_update()
    )
    current = datetime.now(UTC)
    if (
        transaction is None
        or transaction.consumed_at is not None
        or as_utc(transaction.expires_at) <= current
    ):
        raise AppError(401, "invalid_oidc_state", "登录 state 无效或已过期")
    if not secrets.compare_digest(transaction.browser_binding_hash, digest(binding)):
        raise AppError(401, "login_binding_mismatch", "登录浏览器绑定不匹配")
    if exact_redirect_uri(request, settings) != transaction.redirect_uri:
        raise AppError(401, "callback_mismatch", "登录回调入口不匹配")
    transaction.consumed_at = current
    try:
        verifier = _fernet(settings).decrypt(transaction.pkce_verifier_ciphertext).decode()
    except InvalidToken as exc:
        raise AppError(500, "login_transaction_invalid", "登录事务无法解密") from exc
    metadata = discovery(settings)
    try:
        token_response = httpx.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": transaction.redirect_uri,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.resolved_oidc_client_secret,
                "code_verifier": verifier,
            },
            timeout=5.0,
        )
        token_response.raise_for_status()
        tokens = token_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        db.commit()
        raise AppError(401, "code_exchange_failed", "授权码交换失败") from exc
    claims = _claims(tokens.get("id_token", ""), metadata, settings)
    if digest(str(claims.get("nonce", ""))) != transaction.nonce_hash:
        raise AppError(401, "nonce_mismatch", "ID Token nonce 不匹配")
    groups = claims.get("groups", [])
    if settings.required_group not in groups:
        raise AppError(403, "group_required", "当前用户没有 Ledger 访问权限")
    identity = db.scalar(
        select(LocalIdentity).where(
            LocalIdentity.issuer == settings.oidc_issuer, LocalIdentity.subject == claims["sub"]
        )
    )
    if identity is None:
        identity = LocalIdentity(issuer=settings.oidc_issuer, subject=claims["sub"])
        db.add(identity)
        db.flush()
    identity.last_login_at = current
    handle = secrets.token_urlsafe(48)
    db.add(
        BrowserSession(
            identity_id=identity.id,
            session_hash=digest(handle),
            groups_snapshot=groups,
            expires_at=current + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    db.commit()
    response = RedirectResponse(transaction.return_to, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        handle,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    response.set_cookie(
        CSRF_COOKIE,
        new_csrf_token(),
        secure=True,
        httponly=False,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    response.delete_cookie(BINDING_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    handle = request.cookies.get(SESSION_COOKIE)
    if handle:
        session = db.scalar(
            select(BrowserSession).where(BrowserSession.session_hash == digest(handle))
        )
        if session:
            session.revoked_at = datetime.now(UTC)
            db.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    response.delete_cookie(CSRF_COOKIE, secure=True, httponly=False, samesite="lax", path="/")
    return response
