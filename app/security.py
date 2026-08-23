from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.external import is_lan_bypass, is_same_external_origin
from app.models import BrowserSession, LocalIdentity

SESSION_COOKIE = "__Host-ledger-session"
CSRF_COOKIE = "__Host-ledger-csrf"


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


@dataclass(frozen=True)
class Actor:
    owner_id: str
    actor_type: str = "user"
    scopes: frozenset[str] = frozenset(
        {
            "ledger.read",
            "ledger.capture",
            "ledger.write-draft",
            "ledger.confirm",
            "ledger.integrations",
        }
    )


def _service_tokens(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def current_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Actor:
    if is_lan_bypass(request):
        identity = db.scalar(
            select(LocalIdentity)
            .where(LocalIdentity.enabled.is_(True))
            .order_by(LocalIdentity.created_at.asc())
            .limit(1)
        )
        if identity is None:
            raise AppError(503, "lan_identity_unavailable", "局域网身份尚未初始化")
        return Actor(owner_id=f"{identity.issuer}|{identity.subject}")
    if settings.dev_auth and settings.env != "production":
        owner = request.headers.get("X-Dev-User", "dev-user")
        return Actor(owner_id=owner)
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        for owner, item in _service_tokens(settings.service_token_hashes_file).items():
            if hmac.compare_digest(str(item.get("sha256", "")), token_hash):
                return Actor(owner, "service", frozenset(item.get("scopes", [])))
        raise AppError(401, "invalid_token", "服务凭据无效")
    handle = request.cookies.get(SESSION_COOKIE)
    if not handle:
        raise AppError(401, "authentication_required", "请先登录")
    row = db.scalar(
        select(BrowserSession).where(
            BrowserSession.session_hash == digest(handle),
            BrowserSession.revoked_at.is_(None),
            BrowserSession.expires_at > datetime.now(UTC),
        )
    )
    if row is None:
        raise AppError(401, "session_expired", "登录已失效")
    identity = db.get(LocalIdentity, row.identity_id)
    if identity is None or not identity.enabled:
        raise AppError(403, "identity_disabled", "用户不可用")
    row.last_seen_at = datetime.now(UTC)
    db.commit()
    return Actor(owner_id=f"{identity.issuer}|{identity.subject}")


def require_scope(scope: str):
    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if scope not in actor.scopes:
            raise AppError(403, "insufficient_scope", "缺少所需权限", {"scope": scope})
        return actor

    return dependency


def validate_csrf(request: Request, settings: Settings) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if is_lan_bypass(request):
        if is_same_external_origin(request):
            return
        raise AppError(403, "invalid_origin", "请求来源不受信任")
    if settings.dev_auth and settings.env != "production":
        return
    if request.headers.get("Authorization", "").startswith("Bearer "):
        return
    origin = request.headers.get("Origin")
    if origin not in settings.allowed_origins:
        raise AppError(403, "invalid_origin", "请求来源不受信任")
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise AppError(403, "csrf_failed", "CSRF 校验失败")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
