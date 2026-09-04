"""Ledger-specific canonical JSON. This is not RFC 8785 or a signature format."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal


def aware(value: datetime) -> datetime:
    # SQLite strips offsets on stored TIMESTAMPTZ; all stored instants are UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def canonical(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, float):
        raise ValueError("binary floats are not canonical financial values")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp requires offset")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("keys must be strings")
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("unsupported canonical value")


def content_hash(value) -> str:
    # Preserve raw Unicode, null vs missing and array order; never rewrite source text.
    encoded = json.dumps(
        canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


POLICY_VERSION = "ledger-agent-policy-v1"
POLICY_DIGEST = content_hash(
    {
        "version": POLICY_VERSION,
        "l2": "human-exact-grant",
        "draft": "money-only",
        "disclosure": "v1",
    }
)


def capability_hash(grant) -> str:
    return content_hash(
        {
            key: getattr(grant, key)
            for key in (
                "id",
                "owner_id",
                "agent_id",
                "allow_summary",
                "allow_records",
                "allow_budgets",
                "allow_drafts",
                "allow_confirm",
                "active",
            )
        }
        | {"updated_at": aware(grant.updated_at)}
    )
