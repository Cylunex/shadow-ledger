"""Official SDK transport, with the same Ledger gateway on every call."""

from __future__ import annotations

from contextvars import ContextVar

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import db as database
from app.errors import AppError
from app.machine import _require_agent
from app.services.agent_gateway import (
    call_tool,
    compile_catalog,
    local_context,
    machine_context,
    skill_instructions,
)
from app.services.intake import read_owner_id

request_identity = ContextVar("ledger_mcp_identity", default=None)


class LedgerGatewayMCP(MCPServer):
    def __init__(self, skill, *, local=None):
        super().__init__("Shadow Ledger", version="1.3.0", instructions=skill_instructions(skill))
        self.skill = skill
        self.local = local

    def ledger_context(self, db):
        if self.local:
            return self.local
        identity = request_identity.get()
        if identity is None:
            raise AppError(401, "machine_bearer_required", "需要 Agent Bearer")
        return machine_context(db, identity)

    async def list_tools(self):
        with database.SessionLocal() as db:
            catalog = compile_catalog(db, self.ledger_context(db), self.skill)
            result = []
            for descriptor in catalog["tools"]:
                schema = descriptor["inputSchema"]
                schema["properties"]["catalog_hash"] = {
                    "type": "string",
                    "const": catalog["catalog_hash"],
                }
                schema.setdefault("required", []).append("catalog_hash")
                result.append(
                    Tool(
                        name=descriptor["name"],
                        description=descriptor["description"],
                        inputSchema=schema,
                        annotations=descriptor["annotations"],
                        _meta={"catalog_hash": catalog["catalog_hash"], "skill": self.skill},
                    )
                )
            return result

    async def call_tool(self, name, arguments, context=None):
        args = dict(arguments)
        catalog_hash = args.pop("catalog_hash", "")
        try:
            with database.SessionLocal() as db:
                result = call_tool(
                    db, self.ledger_context(db), self.skill, catalog_hash, name, args
                )
            text = (
                result.get("facts_text")
                or result.get("notice")
                or "以 structuredContent 为准；草稿不是正式记录。"
            )
            return CallToolResult(
                content=[TextContent(type="text", text=text)], structuredContent=result
            )
        except AppError as exc:
            return CallToolResult(
                content=[TextContent(type="text", text=exc.message)],
                structuredContent={"error": {"code": exc.code}},
                isError=True,
            )


class AgentBearerBoundary:
    def __init__(self, app, ledger_app):
        self.app = app
        self.ledger_app = ledger_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        # Cookie, LAN bypass and MCP session identifiers are never Agent credentials.
        try:
            identity = _require_agent(self.ledger_app, request.headers.get("authorization"), "")
        except AppError as exc:
            response = JSONResponse(
                {"error": {"code": exc.code, "message": exc.message}},
                status_code=exc.status,
                headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else {},
            )
            return await response(scope, receive, send)
        token = request_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            request_identity.reset(token)


def mount_remote(app, settings):
    servers = []
    # Separate stable task endpoints, not connection-dependent tool lists.
    for prefix, skill in (
        ("/mcp", "research"),
        ("/mcp-capture", "capture"),
        ("/mcp-steward", "steward"),
    ):
        server = LedgerGatewayMCP(skill)
        transport = server.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            max_request_body_size=settings.max_request_bytes,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    url.split("://", 1)[1].split("/", 1)[0] for url in settings.allowed_origins
                ]
                + ["127.0.0.1:*", "localhost:*"],
                allowed_origins=settings.allowed_origins,
            ),
        )

        # _require_agent expects an object with .app; do not substitute auth state from the mounted app.
        class LedgerRequest:
            pass

        holder = LedgerRequest()
        holder.app = app
        app.mount(prefix, AgentBearerBoundary(transport, holder))
        servers.append(server)
    return servers


def build_local_v2(settings):
    if settings.mcp_owner_id_file is None:
        raise RuntimeError("MCP owner file is required")
    owner = read_owner_id(settings.mcp_owner_id_file)
    database.init_database(settings.resolved_database_url)
    return LedgerGatewayMCP(
        settings.mcp_skill,
        local=local_context(
            owner,
            allow_drafts=settings.mcp_allow_drafts,
            disclosure=settings.mcp_disclosure,
            agent_id="ledger-mcp-v2",
        ),
    )
