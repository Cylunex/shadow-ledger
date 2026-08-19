from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import router as api_router
from app.config import Settings, get_settings
from app.db import database_ready, init_database
from app.errors import AppError, app_error_handler
from app.oidc import router as oidc_router
from app.security import validate_csrf

log = logging.getLogger("ledger")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def create_app(settings: Settings | None = None, database_url: str | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_database(database_url)
        yield

    app = FastAPI(title="Shadow Ledger", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.dependency_overrides[get_settings] = lambda: settings
    allowed_hosts = sorted(
        {urlsplit(url).hostname for url in settings.oidc_callbacks if urlsplit(url).hostname}
    )
    if settings.env != "production":
        allowed_hosts.extend(["testserver", "localhost", "127.0.0.1"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["testserver"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "If-Match", "Idempotency-Key", "X-CSRF-Token"],
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(oidc_router)
    app.include_router(api_router)
    app.add_exception_handler(AppError, app_error_handler)

    @app.middleware("http")
    async def safety_headers(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            content_length = request.headers.get("Content-Length")
            if content_length:
                try:
                    too_large = int(content_length) > settings.max_request_bytes
                except ValueError as exc:
                    raise AppError(400, "invalid_content_length", "Content-Length 无效") from exc
                if too_large:
                    raise AppError(413, "request_too_large", "请求体过大")
            validate_csrf(request, settings)
            response = await call_next(request)
        except AppError as exc:
            response = await app_error_handler(request, exc)
        response.headers["X-Request-ID"] = request_id
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        log.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "route": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
        )
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        if not database_ready():
            return JSONResponse({"status": "not_ready", "database": "unavailable"}, status_code=503)
        return {"status": "ready", "database": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "index.html", {"page": "records"})

    @app.get("/consumption", response_class=HTMLResponse)
    def consumption(request: Request):
        return templates.TemplateResponse(request, "index.html", {"page": "consumption"})

    @app.get("/planning", response_class=HTMLResponse)
    def planning(request: Request):
        return templates.TemplateResponse(request, "index.html", {"page": "planning"})

    @app.get("/insights", response_class=HTMLResponse)
    def insights(request: Request):
        return templates.TemplateResponse(request, "index.html", {"page": "insights"})

    return app


app = create_app()


def run() -> None:
    configure_logging()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        proxy_headers=bool(settings.trusted_proxies),
        forwarded_allow_ips=",".join(settings.trusted_proxies),
    )
