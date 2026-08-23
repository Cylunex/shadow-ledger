from __future__ import annotations

from fastapi import Request


def external_prefix(request: Request) -> str:
    value = str(request.scope.get("x_forwarded_prefix", ""))
    return value if value.startswith("/") and value != "/" else ""


def prefixed(request: Request, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("internal redirect path must be absolute")
    return f"{external_prefix(request)}{path}"
