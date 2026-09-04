from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from sqlalchemy import select

from app import db as database
from app.config import Settings, get_settings
from app.models import ForecastItem, ForecastRun, LedgerRecord, MoneyEntry
from app.payments import PAYMENT_METHOD_LABELS, PaymentMethod
from app.schemas import MoneyEntryInput, RecordCreate, jsonable
from app.services.intake import read_owner_id
from app.services.records import create_record, list_records


def _month_range(month: str | None) -> tuple[datetime, datetime, str]:
    if month is None:
        current = datetime.now(UTC)
        month = f"{current.year:04d}-{current.month:02d}"
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM") from exc
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end, month


def _currency(value: str) -> str:
    result = value.upper()
    if len(result) != 3 or not result.isalpha():
        raise ValueError("currency must be a three-letter code")
    return result


def mcp_summary(owner_id: str, month: str | None, currency: str) -> dict[str, Any]:
    start, end, normalized_month = _month_range(month)
    currency = _currency(currency)
    assert database.SessionLocal is not None
    totals = {"expense": Decimal("0"), "income": Decimal("0"), "refund": Decimal("0")}
    with database.SessionLocal() as session:
        rows = session.execute(
            select(MoneyEntry.type, MoneyEntry.amount)
            .join(LedgerRecord, MoneyEntry.record_id == LedgerRecord.id)
            .where(
                LedgerRecord.owner_id == owner_id,
                LedgerRecord.state == "confirmed",
                LedgerRecord.occurred_at >= start,
                LedgerRecord.occurred_at < end,
                MoneyEntry.currency == currency,
            )
        )
        for money_type, amount in rows:
            totals[money_type] += amount
    return {
        "month": normalized_month,
        "currency": currency,
        **{key: format(value, ".4f") for key, value in totals.items()},
        "net_spending": format(totals["expense"] - totals["refund"], ".4f"),
    }


def mcp_records(
    owner_id: str, month: str | None, limit: int, payment_method: PaymentMethod | None = None,
) -> dict[str, Any]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if payment_method is not None and payment_method not in PAYMENT_METHOD_LABELS:
        raise ValueError("invalid payment method")
    start, end, normalized_month = _month_range(month)
    assert database.SessionLocal is not None
    with database.SessionLocal() as session:
        rows = list_records(
            session,
            owner_id,
            state="confirmed",
            occurred_from=start,
            occurred_to=end,
            limit=limit,
            payment_method=payment_method,
        )
        items = [
            jsonable(
                {
                    "uri": f"shadow://ledger/records/{row.id}",
                    "occurred_at": row.occurred_at,
                    "money_type": row.money_entry.type if row.money_entry else None,
                    "amount": row.money_entry.amount if row.money_entry else None,
                    "currency": row.money_entry.currency if row.money_entry else None,
                    "title": row.money_entry.title if row.money_entry else None,
                    "payment_method": row.money_entry.payment_method if row.money_entry else None,
                    "scene": row.consumption.scene if row.consumption else None,
                }
            )
            for row in rows
        ]
    return {"month": normalized_month, "items": items}


def mcp_forecasts(owner_id: str, limit: int) -> dict[str, Any]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    assert database.SessionLocal is not None
    with database.SessionLocal() as session:
        run = session.scalar(
            select(ForecastRun)
            .where(ForecastRun.owner_id == owner_id)
            .order_by(ForecastRun.created_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        if run is None:
            return {"run": None, "items": []}
        rows = session.scalars(
            select(ForecastItem)
            .where(ForecastItem.run_id == run.id)
            .order_by(ForecastItem.predicted_at, ForecastItem.source_key)
        ).all()
        from app.services.feedback import effective_feedback, feedback_map
        decisions = feedback_map(session, owner_id, rows)
        rows = [row for row in rows if effective_feedback(row, decisions)["state"] == "active"][:limit]
        items = [
            jsonable(
                {
                    "kind": row.kind,
                    "target_uri": row.target_uri,
                    "predicted_at": row.predicted_at,
                    "expected_amount": row.expected_amount,
                    "currency": row.currency,
                    "confidence": row.confidence,
                    "explanation": row.explanation,
                }
            )
            for row in rows
        ]
        return {
            "run": {
                "id": str(run.id),
                "as_of": run.as_of.isoformat(),
                "timezone": run.timezone,
                "algorithm_version": run.algorithm_version,
            },
            "items": items,
        }


def mcp_create_draft(
    owner_id: str,
    *,
    idempotency_key: str,
    money_type: Literal["expense", "income", "refund"],
    amount: str,
    currency: str,
    title: str = "",
    occurred_at: str | None = None,
    timezone: str = "Asia/Shanghai",
    payment_method: PaymentMethod | None = None,
) -> dict[str, Any]:
    if not idempotency_key or len(idempotency_key) > 196:
        raise ValueError("idempotency_key is required and must be at most 196 characters")
    try:
        parsed_amount = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("amount must be a decimal string") from exc
    when = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("occurred_at must include a UTC offset")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA name") from exc
    candidate = RecordCreate(
        occurred_at=when,
        timezone=timezone,
        money_entry=MoneyEntryInput(
            type=money_type,
            amount=parsed_amount,
            currency=_currency(currency),
            title=title,
            payment_method=payment_method,
        ),
        confirm=False,
    )
    assert database.SessionLocal is not None
    with database.SessionLocal() as session:
        record = create_record(
            session,
            owner_id,
            candidate,
            f"mcp:{idempotency_key}",
            "ledger-mcp",
            actor_type="service",
        )
        return {
            "uri": f"shadow://ledger/records/{record.id}",
            "state": record.state,
            "revision": record.revision,
            "message": "草稿已创建，必须由用户审核后才能成为正式记录。",
        }


def build_mcp_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or get_settings()
    if settings.mcp_agent_v2:
        from app.agent_mcp import build_local_v2
        return build_local_v2(settings)
    if settings.mcp_owner_id_file is None:
        raise RuntimeError("LEDGER_MCP_OWNER_ID_FILE is required")
    owner_id = read_owner_id(settings.mcp_owner_id_file)
    database.init_database(settings.resolved_database_url)
    server = MCPServer(
        "Shadow Ledger",
        instructions=(
            "读取个人收支与可解释预测。写入能力只能创建 draft，绝不能确认、撤销、导出或执行资金操作。"
        ),
    )

    @server.tool(title="月度收支摘要", structured_output=True)
    def ledger_monthly_summary(month: str | None = None, currency: str = "CNY") -> dict[str, Any]:
        """读取指定月份、单一币种的已确认收支摘要，不做汇率换算。"""

        return mcp_summary(owner_id, month, currency)

    @server.tool(title="已确认记录", structured_output=True)
    def ledger_records(
        month: str | None = None, limit: int = 20, payment_method: PaymentMethod | None = None,
    ) -> dict[str, Any]:
        """读取最小披露的已确认记录；不返回备注、原始抓单正文或凭据。"""

        return mcp_records(owner_id, month, limit, payment_method)

    @server.tool(title="消费预测建议", structured_output=True)
    def ledger_forecasts(limit: int = 20) -> dict[str, Any]:
        """读取最近一次已生成的可解释预测；预测不是消费事实。"""

        return mcp_forecasts(owner_id, limit)

    if settings.mcp_allow_drafts:

        @server.tool(title="创建待审核草稿", structured_output=True)
        def ledger_create_draft(
            idempotency_key: str,
            money_type: Literal["expense", "income", "refund"],
            amount: str,
            currency: str = "CNY",
            title: str = "",
            occurred_at: str | None = None,
            timezone: str = "Asia/Shanghai",
            payment_method: PaymentMethod | None = None,
        ) -> dict[str, Any]:
            """创建 money-only 草稿。该工具永远不会确认正式事实。"""

            return mcp_create_draft(
                owner_id,
                idempotency_key=idempotency_key,
                money_type=money_type,
                amount=amount,
                currency=currency,
                title=title,
                occurred_at=occurred_at,
                timezone=timezone,
                payment_method=payment_method,
            )

    return server


def run() -> None:
    build_mcp_server().run(transport="stdio")
