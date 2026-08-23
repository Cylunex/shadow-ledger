from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import Request


def external_prefix(request: Request) -> str:
    value = str(request.scope.get("x_forwarded_prefix", ""))
    return value if value.startswith("/") and value != "/" else ""


def prefixed(request: Request, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("internal redirect path must be absolute")
    return f"{external_prefix(request)}{path}"


def is_lan_bypass(request: Request, expected_prefix: str = "/ledger") -> bool:
    if request.headers.get("X-Shadow-Lan-Bypass", "") != "1":
        return False
    if external_prefix(request) != expected_prefix:
        return False
    host = urlsplit(f"//{request.headers.get('Host', '')}").hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_same_external_origin(request: Request) -> bool:
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return False
    actual = urlsplit(origin)
    expected_scheme = request.headers.get("X-Forwarded-Proto", "").strip()
    expected_scheme = expected_scheme or request.url.scheme
    expected = urlsplit(f"{expected_scheme}://{request.headers.get('Host', '')}")
    return (
        actual.scheme == expected.scheme
        and actual.hostname == expected.hostname
        and (actual.port or (443 if actual.scheme == "https" else 80))
        == (expected.port or (443 if expected.scheme == "https" else 80))
    )
