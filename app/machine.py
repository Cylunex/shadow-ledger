from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import Field, field_validator
from shadow_sdk.agent import AgentIdentity
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.agent import AgentAccess, MachineAuthError, MachineAuthUnavailable, MachineScopeError
from app.db import get_db
from app.errors import AppError
from app.models import (
    AuditEvent,
    BudgetTarget,
    ConsumptionEvent,
    ExternalReference,
    LedgerAgentGrant,
    LedgerRecord,
    MoneyCategory,
    MoneyEntry,
)
from app.payments import PaymentMethod
from app.schemas import (
    ConsumptionInput,
    ConsumptionLineInput,
    Money,
    MoneyEntryInput,
    RecordCreate,
    StrictModel,
    jsonable,
)
from app.services.records import create_record, get_record, idempotency_lookup

router = APIRouter(prefix="/api/machine/v1/agent", tags=["machine-agent"])


class AgentRecordDraftCreate(StrictModel):
    occurred_at: datetime
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    money_type: Literal["expense", "income", "refund"]
    amount: Money
    currency: str = Field(min_length=3, max_length=3)
    category_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,49}$")
    title: str = Field(default="", max_length=160)
    payment_method: PaymentMethod | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_input(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("amount must be a decimal string, not binary float")
        return value

    @field_validator("occurred_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a known IANA timezone") from exc
        return value

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized.isascii() or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code")
        return normalized


class AgentRecordDraftCommit(StrictModel):
    revision: int = Field(ge=1)
    approval_grant_id: uuid.UUID | None = None


class NexusReviewCreate(StrictModel):
    intent: str = Field(pattern=r"^ledger\.[A-Za-z0-9.-]{1,80}$")
    summary: str = Field(min_length=1, max_length=500)
    fields: dict[str, object]
    source_text: str = Field(default="", max_length=4000)
    source_refs: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("source_refs")
    @classmethod
    def valid_source_refs(cls, value: list[str]) -> list[str]:
        if any(
            len(item) > 1024 or re.fullmatch(r"shadow://[a-z][a-z0-9-]{1,63}/.+", item) is None
            for item in value
        ):
            raise ValueError("source_refs must contain valid shadow:// URIs")
        return list(dict.fromkeys(value))


class NexusLedgerCommand(StrictModel):
    protocol: Literal["shadow.command.v1"]
    command_id: str = Field(pattern=r"^cmd_[A-Za-z0-9_-]{8,128}$")
    capability_ref: str = Field(pattern=r"^shadow://capabilities/.+/ledger\.records\.write$")
    operation_id: Literal["execute_nexus_ledger_command"]
    schema_version: Literal[1]
    arguments: NexusReviewCreate
    target_refs: list[str] = Field(default_factory=list, max_length=16)
    source_refs: list[str] = Field(default_factory=list, max_length=16)


def _bearer_error(status_code: int, code: str, message: str) -> AppError:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return AppError(status_code, code, message, headers=headers)


def _require_agent(request: Request, authorization: str | None, scope: str) -> AgentIdentity:
    if not authorization:
        raise _bearer_error(401, "machine_bearer_required", "需要 Ledger Agent Bearer 凭据")
    access: AgentAccess = request.app.state.agent_access
    try:
        return access.authenticate(authorization, scope=scope)
    except MachineAuthUnavailable as exc:
        raise AppError(503, "agent_auth_unavailable", "Ledger Agent 鉴权暂不可用") from exc
    except MachineScopeError as exc:
        raise _bearer_error(403, "machine_scope_forbidden", "Ledger Agent scope 未授权") from exc
    except MachineAuthError as exc:
        raise _bearer_error(401, "machine_bearer_invalid", "Ledger Agent 凭据无效") from exc


def _grant(db: Session, identity: AgentIdentity, permission: str) -> LedgerAgentGrant:
    grant = db.scalar(
        select(LedgerAgentGrant).where(
            LedgerAgentGrant.agent_id == identity.agent_id,
            LedgerAgentGrant.active.is_(True),
        )
    )
    if grant is None:
        raise AppError(404, "ledger_grant_not_found", "Ledger 资源授权不存在")
    if not bool(getattr(grant, permission)):
        raise AppError(403, "ledger_grant_forbidden", "Ledger 资源操作未授权")
    return grant


def _owned_agent_draft(
    db: Session,
    grant: LedgerAgentGrant,
    identity: AgentIdentity,
    record_id: uuid.UUID,
) -> LedgerRecord:
    created_by_agent = db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.owner_id == grant.owner_id,
            AuditEvent.aggregate_type == "record",
            AuditEvent.aggregate_id == record_id,
            AuditEvent.action == "record.created",
            AuditEvent.actor_type == "agent",
            AuditEvent.actor_id == identity.agent_id,
        )
    )
    if created_by_agent is None:
        raise AppError(404, "agent_draft_not_found", "Agent 草稿不存在")
    return get_record(db, grant.owner_id, record_id)


def _month_range(month: str | None) -> tuple[datetime, datetime, str]:
    try:
        start = (
            datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
            if month
            else datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
    except ValueError as exc:
        raise AppError(400, "invalid_month", "月份格式应为 YYYY-MM") from exc
    return start, start + relativedelta(months=1), start.strftime("%Y-%m")


def _currency(value: str) -> str:
    normalized = value.upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise AppError(422, "invalid_currency", "币种必须是三个 ASCII 字母")
    return normalized


def _audit_read(
    db: Session,
    grant: LedgerAgentGrant,
    identity: AgentIdentity,
    action: str,
    details: dict[str, object],
) -> None:
    db.add(
        AuditEvent(
            owner_id=grant.owner_id,
            actor_type="agent",
            actor_id=identity.agent_id,
            action=action,
            aggregate_type="ledger_agent_grant",
            aggregate_id=grant.id,
            details=details,
        )
    )
    db.commit()


@router.get("/summary")
def agent_summary(
    request: Request,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    currency: str = Query(default="CNY", min_length=3, max_length=3),
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.summary.read")
    grant = _grant(db, identity, "allow_summary")
    start, end, normalized_month = _month_range(month)
    normalized_currency = _currency(currency)
    expense, income, refund = map(
        Decimal,
        db.execute(
            select(
                func.coalesce(
                    func.sum(case((MoneyEntry.type == "expense", MoneyEntry.amount), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((MoneyEntry.type == "income", MoneyEntry.amount), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((MoneyEntry.type == "refund", MoneyEntry.amount), else_=0)), 0
                ),
            )
            .join(LedgerRecord)
            .where(
                LedgerRecord.owner_id == grant.owner_id,
                LedgerRecord.state == "confirmed",
                LedgerRecord.occurred_at >= start,
                LedgerRecord.occurred_at < end,
                MoneyEntry.currency == normalized_currency,
            )
        ).one(),
    )
    response = jsonable(
        {
            "month": normalized_month,
            "currency": normalized_currency,
            "expense": expense,
            "income": income,
            "refund": refund,
            "net_spending": expense - refund,
            "generated_at": datetime.now(UTC),
            "exchange_rate_applied": False,
        }
    )
    _audit_read(
        db,
        grant,
        identity,
        "agent.summary.read",
        {"month": normalized_month, "currency": normalized_currency},
    )
    return response


@router.get("/records")
def agent_records(
    request: Request,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    limit: int = Query(default=25, ge=1, le=50),
    payment_method: PaymentMethod | None = None,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.records.read")
    grant = _grant(db, identity, "allow_records")
    start, end, normalized_month = _month_range(month)
    rows = db.execute(
        select(
            LedgerRecord.id,
            LedgerRecord.occurred_at,
            LedgerRecord.record_kind,
            MoneyEntry.type,
            MoneyEntry.amount,
            MoneyEntry.currency,
            MoneyCategory.key,
            ConsumptionEvent.scene,
            MoneyEntry.payment_method,
        )
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .outerjoin(MoneyCategory, MoneyCategory.id == MoneyEntry.category_id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == grant.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            *([MoneyEntry.payment_method == payment_method] if payment_method else []),
        )
        .order_by(LedgerRecord.occurred_at.desc(), LedgerRecord.id)
        .limit(limit + 1)
    ).all()
    truncated = len(rows) > limit
    rows = rows[:limit]
    response = jsonable(
        {
            "month": normalized_month,
            "items": [
                {
                    "record_ref": f"shadow://ledger/records/{record_id}",
                    "occurred_at": occurred_at,
                    "record_kind": record_kind,
                    "money_type": money_type,
                    "amount": amount,
                    "currency": currency,
                    "category_key": category_key,
                    "scene": scene,
                    "payment_method": method,
                }
                for (
                    record_id,
                    occurred_at,
                    record_kind,
                    money_type,
                    amount,
                    currency,
                    category_key,
                    scene,
                    method,
                ) in rows
            ],
            "truncated": truncated,
        }
    )
    _audit_read(
        db,
        grant,
        identity,
        "agent.records.read",
        {"month": normalized_month, "result_count": len(rows), "truncated": truncated},
    )
    return response


@router.get("/budgets")
def agent_budgets(
    request: Request,
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.budgets.read")
    grant = _grant(db, identity, "allow_budgets")
    start, end, normalized_month = _month_range(month)
    targets = db.execute(
        select(BudgetTarget, MoneyCategory.key)
        .outerjoin(MoneyCategory, MoneyCategory.id == BudgetTarget.category_id)
        .where(
            BudgetTarget.owner_id == grant.owner_id,
            BudgetTarget.budget_month == start.date(),
            BudgetTarget.active.is_(True),
        )
        .order_by(BudgetTarget.currency, MoneyCategory.key)
    ).all()
    items: list[dict[str, object]] = []
    for target, category_key in targets:
        statement = (
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (MoneyEntry.type == "refund", -MoneyEntry.amount),
                            else_=MoneyEntry.amount,
                        )
                    ),
                    0,
                )
            )
            .join(LedgerRecord)
            .where(
                LedgerRecord.owner_id == grant.owner_id,
                LedgerRecord.state == "confirmed",
                LedgerRecord.occurred_at >= start,
                LedgerRecord.occurred_at < end,
                MoneyEntry.currency == target.currency,
                MoneyEntry.type.in_(["expense", "refund"]),
            )
        )
        if target.category_id:
            statement = statement.where(MoneyEntry.category_id == target.category_id)
        spent = Decimal(db.scalar(statement) or 0)
        items.append(
            {
                "budget_ref": f"shadow://ledger/budget-targets/{target.id}",
                "category_key": category_key,
                "monthly_amount": target.monthly_amount,
                "currency": target.currency,
                "net_spending": spent,
                "remaining": target.monthly_amount - spent,
            }
        )
    unknown = db.scalar(
        select(func.count())
        .select_from(LedgerRecord)
        .join(ConsumptionEvent)
        .where(
            LedgerRecord.owner_id == grant.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            ConsumptionEvent.money_entry_id.is_(None),
        )
    )
    response = jsonable(
        {
            "month": normalized_month,
            "items": items,
            "unknown_amount_records": int(unknown or 0),
            "exchange_rate_applied": False,
        }
    )
    _audit_read(
        db,
        grant,
        identity,
        "agent.budgets.read",
        {"month": normalized_month, "result_count": len(items)},
    )
    return response


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
def agent_draft_create(
    body: AgentRecordDraftCreate,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.records.draft")
    grant = _grant(db, identity, "allow_drafts")
    if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
        raise AppError(400, "idempotency_key_required", "Idempotency-Key 长度必须为 8 到 128")
    data = RecordCreate(
        occurred_at=body.occurred_at,
        timezone=body.timezone,
        money_entry=MoneyEntryInput(
            type=body.money_type,
            amount=body.amount,
            currency=body.currency,
            category_key=body.category_key,
            title=body.title,
            payment_method=body.payment_method,
        ),
        confirm=False,
    )
    record = create_record(
        db,
        grant.owner_id,
        data,
        f"agent:{identity.agent_id}:{idempotency_key}",
        identity.agent_id,
        actor_type="agent",
    )
    return {
        "record_ref": f"shadow://ledger/records/{record.id}",
        "state": "draft",
        "revision": record.revision,
        "reversible": True,
        "final_entry_created": False,
    }


@router.get("/drafts")
def agent_draft_list(
    request: Request,
    limit: int = Query(default=200, ge=1, le=200),
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.records.write")
    grant = _grant(db, identity, "allow_confirm")
    rows = list(
        db.scalars(
            select(LedgerRecord)
            .join(
                AuditEvent,
                (AuditEvent.aggregate_type == "record")
                & (AuditEvent.aggregate_id == LedgerRecord.id)
                & (AuditEvent.action == "record.created")
                & (AuditEvent.actor_type == "agent")
                & (AuditEvent.actor_id == identity.agent_id),
            )
            .where(
                LedgerRecord.owner_id == grant.owner_id,
                LedgerRecord.state == "draft",
            )
            .order_by(LedgerRecord.created_at, LedgerRecord.id)
            .limit(limit + 1)
        )
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    items: list[dict[str, object]] = []
    for record in rows:
        entry = record.money_entry
        category = db.get(MoneyCategory, entry.category_id) if entry and entry.category_id else None
        items.append(
            {
                "record_ref": f"shadow://ledger/records/{record.id}",
                "revision": record.revision,
                "created_at": record.created_at,
                "occurred_at": record.occurred_at,
                "money_type": entry.type if entry else None,
                "amount": entry.amount if entry else None,
                "currency": entry.currency if entry else None,
                "category_key": category.key if category else None,
                "title": entry.title if entry else "",
            }
        )
    return jsonable({"items": items, "truncated": truncated})


@router.post("/drafts/{record_id}/commit")
def agent_draft_commit(
    record_id: uuid.UUID,
    body: AgentRecordDraftCommit,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.records.write")
    grant = _grant(db, identity, "allow_confirm")
    if body.approval_grant_id is None:
        raise AppError(428, "approval_required", "需要用户在 Ledger 审核精确内容后签发的一次性批准")
    from app.services.agent_effects import execute
    result = execute(db, grant.owner_id, identity.agent_id, body.approval_grant_id,
                   record_id=record_id, revision=body.revision, action="confirm")
    return {key: result[key] for key in ("record_ref", "state", "revision", "replayed", "final_entry_created", "receipt")}


@router.post("/drafts/{record_id}/reject", status_code=status.HTTP_200_OK)
def agent_draft_reject(
    record_id: uuid.UUID,
    body: AgentRecordDraftCommit,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    identity = _require_agent(request, authorization, "ledger.records.write")
    grant = _grant(db, identity, "allow_confirm")
    if body.approval_grant_id is None:
        raise AppError(428, "approval_required", "删除草稿也需要用户精确批准")
    from app.services.agent_effects import execute
    result = execute(db, grant.owner_id, identity.agent_id, body.approval_grant_id,
                   record_id=record_id, revision=body.revision, action="reject")
    return {key: result[key] for key in ("record_ref", "state", "replayed", "receipt")}


def _review_text(fields: dict[str, object], key: str, *, maximum: int) -> str | None:
    value = fields.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise AppError(422, "invalid_nexus_review", f"{key} 字段无效")
    return value or None


def _review_consumption(fields: dict[str, object]) -> ConsumptionInput | None:
    keys = {
        "scene",
        "merchantNameRaw",
        "channelKey",
        "channelNameRaw",
        "placeRef",
        "consumptionNote",
        "consumptionItemsJson",
    }
    if not any(key in fields for key in keys):
        return None
    scene = fields.get("scene")
    if not isinstance(scene, str) or not scene:
        raise AppError(422, "invalid_nexus_review", "消费草稿缺少 scene")
    allowed_scenes = (
        "online_purchase",
        "offline_purchase",
        "delivery",
        "dine_in",
        "drink",
        "service",
        "subscription",
        "transport",
        "entertainment",
        "travel",
        "other",
    )
    if scene not in allowed_scenes:
        raise AppError(
            422,
            "invalid_nexus_review",
            "scene 字段无效",
            {"field": "scene", "value": scene, "allowed_values": list(allowed_scenes)},
        )

    raw_items = fields.get("consumptionItemsJson", [])
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except json.JSONDecodeError as exc:
            raise AppError(
                422, "invalid_nexus_review", "consumptionItemsJson 不是有效 JSON"
            ) from exc
    if not isinstance(raw_items, list) or len(raw_items) > 100:
        raise AppError(422, "invalid_nexus_review", "消费明细必须是最多 100 项的数组")

    lines: list[ConsumptionLineInput] = []
    try:
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError("line must be an object")
            raw_name = item.get("rawName", item.get("raw_name"))
            lines.append(
                ConsumptionLineInput(
                    raw_name=raw_name,
                    quantity=item.get("quantity"),
                    unit=item.get("unit"),
                    amount=item.get("amount"),
                    content_category=item.get("contentCategory", item.get("content_category")),
                    note=item.get("note", ""),
                    sort_order=item.get("sortOrder", item.get("sort_order", index)),
                )
            )
        return ConsumptionInput(
            scene=scene,
            merchant_name_raw=_review_text(fields, "merchantNameRaw", maximum=1000),
            channel_key=_review_text(fields, "channelKey", maximum=50),
            channel_name_raw=_review_text(fields, "channelNameRaw", maximum=500),
            place_ref=_review_text(fields, "placeRef", maximum=1024),
            note=_review_text(fields, "consumptionNote", maximum=2000) or "",
            lines=lines,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(422, "invalid_nexus_review", "消费草稿字段无效") from exc


def _sync_nexus_source_refs(
    db: Session, owner_id: str, record_id: uuid.UUID, source_refs: list[str]
) -> None:
    existing = set(
        db.scalars(
            select(ExternalReference.target_uri).where(
                ExternalReference.owner_id == owner_id,
                ExternalReference.source_type == "record",
                ExternalReference.source_id == record_id,
                ExternalReference.relation == "evidence",
            )
        )
    )
    requested = set(source_refs)
    if existing and existing != requested:
        raise AppError(409, "idempotency_mismatch", "同一草稿的来源引用不能改变")
    if existing:
        return
    for target_uri in source_refs:
        db.add(
            ExternalReference(
                owner_id=owner_id,
                source_type="record",
                source_id=record_id,
                relation="evidence",
                target_uri=target_uri,
            )
        )


@router.post(
    "/nexus/reviews",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_nexus_ledger_review",
)
def create_nexus_ledger_review(
    body: NexusReviewCreate,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    fields = body.fields
    occurred_at = fields.get("occurredAt")
    money_type = fields.get("moneyType")
    amount = fields.get("amount")
    currency = fields.get("currency", "CNY")
    if not isinstance(occurred_at, str):
        occurred_at = datetime.now(UTC).isoformat()
    if money_type not in {"expense", "income", "refund"} or amount is None:
        raise AppError(422, "invalid_nexus_review", "账目草稿缺少金额或收支类型")
    identity = _require_agent(request, authorization, "ledger.records.draft")
    grant = _grant(db, identity, "allow_drafts")
    if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
        raise AppError(400, "idempotency_key_required", "Idempotency-Key 长度必须为 8 到 128")
    try:
        candidate = RecordCreate(
            occurred_at=datetime.fromisoformat(occurred_at),
            timezone=str(fields.get("timezone") or "Asia/Shanghai"),
            money_entry=MoneyEntryInput(
                type=money_type,
                amount=amount,
                currency=str(currency),
                category_key=(
                    str(fields["categoryKey"]) if fields.get("categoryKey") is not None else None
                ),
                title=str(fields.get("title") or body.summary),
                payment_method=_review_text(fields, "paymentMethod", maximum=24),
            ),
            consumption=_review_consumption(fields),
            confirm=False,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(422, "invalid_nexus_review", "账目草稿字段无效") from exc
    record = create_record(
        db,
        grant.owner_id,
        candidate,
        f"agent:{identity.agent_id}:{idempotency_key}",
        identity.agent_id,
        actor_type="agent",
        commit=False,
    )
    _sync_nexus_source_refs(db, grant.owner_id, record.id, body.source_refs)
    db.commit()
    record = get_record(db, grant.owner_id, record.id)
    return _ledger_review_envelope(record, db, request.state.request_id)


@router.post("/nexus/commands", operation_id="execute_nexus_ledger_command")
def execute_nexus_ledger_command(
    command: NexusLedgerCommand,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Commit one ordinary private Ledger record under the caller's current intent."""
    identity = _require_agent(request, authorization, "ledger.records.write")
    grant = _grant(db, identity, "allow_confirm")
    if not grant.allow_drafts:
        raise AppError(403, "ledger_grant_forbidden", "Ledger 资源操作未授权")
    body = command.arguments
    fields = body.fields
    occurred_at = fields.get("occurredAt")
    if not isinstance(occurred_at, str):
        occurred_at = datetime.now(UTC).isoformat()
    money_type = fields.get("moneyType")
    amount = fields.get("amount")
    intent_matches_record = body.intent == "ledger.record" or body.intent.startswith("ledger.record.")
    expected_intent = f"ledger.{money_type}"
    intent_matches_money_type = body.intent == expected_intent or body.intent.startswith(f"{expected_intent}.")
    if not (intent_matches_record or intent_matches_money_type):
        raise AppError(422, "invalid_nexus_command", "账目命令意图与收支类型不一致")
    if money_type not in {"expense", "income", "refund"} or amount is None:
        raise AppError(422, "invalid_nexus_command", "账目命令缺少金额或收支类型")
    try:
        candidate = RecordCreate(
            occurred_at=datetime.fromisoformat(occurred_at),
            timezone=str(fields.get("timezone") or "Asia/Shanghai"),
            money_entry=MoneyEntryInput(
                type=money_type,
                amount=amount,
                currency=str(fields.get("currency", "CNY")),
                category_key=str(fields["categoryKey"]) if fields.get("categoryKey") is not None else None,
                title=str(fields.get("title") or body.summary),
                payment_method=_review_text(fields, "paymentMethod", maximum=24),
            ),
            consumption=_review_consumption(fields),
            confirm=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(422, "invalid_nexus_command", "账目命令字段无效") from exc
    key = f"agent:{identity.agent_id}:{command.command_id}"
    replayed = idempotency_lookup(db, grant.owner_id, "records.create", key, candidate.model_dump()) is not None
    record = create_record(
        db, grant.owner_id, candidate, key, identity.agent_id, actor_type="agent", commit=False
    )
    _sync_nexus_source_refs(db, grant.owner_id, record.id, body.source_refs)
    db.commit()
    return jsonable({
        "protocol": "shadow.execution-result.v1",
        "command_id": command.command_id,
        "capability_ref": command.capability_ref,
        "operation_id": command.operation_id,
        "status": "committed",
        "result_kind": "record",
        "resource_ref": f"shadow://ledger/records/{record.id}",
        "receipt_ref": f"shadow://ledger/operations/{command.command_id}",
        "completed_at": record.updated_at,
        "replayed": replayed,
        "summary": "账目已保存。",
        "fields": {"state": record.state, "revision": record.revision},
    })


@router.get("/nexus/reviews", operation_id="list_nexus_ledger_reviews")
def list_nexus_ledger_reviews(
    request: Request,
    limit: int = Query(default=200, ge=1, le=200),
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    listed = agent_draft_list(request, limit, authorization, db)
    items = []
    for item in listed["items"]:
        record_id = uuid.UUID(str(item["record_ref"]).rsplit("/", 1)[-1])
        record = db.get(LedgerRecord, record_id)
        if record is not None:
            items.append(_ledger_review_envelope(record, db, request.state.request_id))
    return {
        "protocol": "shadow.review.v1",
        "items": items,
        "truncated": listed["truncated"],
        "trace_id": request.state.request_id,
    }


@router.post(
    "/nexus/reviews/{review_id}/commit",
    operation_id="commit_nexus_ledger_review",
)
def commit_nexus_ledger_review(
    review_id: uuid.UUID,
    body: AgentRecordDraftCommit,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    result = agent_draft_commit(review_id, body, request, authorization, db)
    record = db.get(LedgerRecord, review_id)
    if record is None:
        raise AppError(500, "nexus_review_missing", "账目记录提交后不可见")
    return _ledger_review_envelope(
        record,
        db,
        request.state.request_id,
        receipt=str(result["receipt"]),
        replayed=bool(result["replayed"]),
    )


@router.post(
    "/nexus/reviews/{review_id}/reject",
    operation_id="reject_nexus_ledger_review",
)
def reject_nexus_ledger_review(
    review_id: uuid.UUID,
    body: AgentRecordDraftCommit,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    result = agent_draft_reject(review_id, body, request, authorization, db)
    return {
        "protocol": "shadow.review.v1",
        "review_id": str(review_id),
        "reference": str(result["record_ref"]),
        "revision": body.revision,
        "domain": "ledger",
        "intent": "ledger.record",
        "summary": "Ledger 草稿已退回",
        "fields": {},
        "risk_level": "L2",
        "state": "rejected",
        "created_at": datetime.now(UTC).isoformat(),
        "source_refs": [],
        "trace_id": request.state.request_id,
        "receipt": result["receipt"],
        "replayed": bool(result["replayed"]),
    }


def _ledger_review_envelope(
    record: LedgerRecord,
    db: Session,
    trace_id: str,
    *,
    receipt: str | None = None,
    replayed: bool = False,
) -> dict[str, object]:
    entry = record.money_entry
    category = db.get(MoneyCategory, entry.category_id) if entry and entry.category_id else None
    fields: dict[str, object] = {
        "occurredAt": record.occurred_at.isoformat(),
        "timezone": record.timezone,
    }
    if entry is not None:
        fields.update(
            {
                "moneyType": entry.type,
                "amount": str(entry.amount),
                "currency": entry.currency,
                "title": entry.title,
                "paymentMethod": entry.payment_method,
            }
        )
    if category is not None:
        fields["categoryKey"] = category.key
    consumption = record.consumption
    if consumption is not None:
        fields.update(
            {
                "scene": consumption.scene,
                "merchantNameRaw": consumption.merchant_name_raw,
                "channelKey": consumption.channel_key,
                "channelNameRaw": consumption.channel_name_raw,
                "placeRef": consumption.place_ref,
                "consumptionNote": consumption.note,
                "consumptionItemsJson": [
                    {
                        "rawName": line.raw_name,
                        "quantity": line.quantity,
                        "unit": line.unit,
                        "amount": line.amount,
                        "contentCategory": line.content_category,
                        "note": line.note,
                        "sortOrder": line.sort_order,
                    }
                    for line in sorted(consumption.lines, key=lambda item: item.sort_order)
                ],
            }
        )
        fields = {key: value for key, value in fields.items() if value is not None}
    source_refs = list(
        db.scalars(
            select(ExternalReference.target_uri)
            .where(
                ExternalReference.owner_id == record.owner_id,
                ExternalReference.source_type == "record",
                ExternalReference.source_id == record.id,
                ExternalReference.relation == "evidence",
            )
            .order_by(ExternalReference.target_uri)
        )
    )
    state = "committed" if record.state == "confirmed" else "pending"
    summary = str(fields.get("title") or "Ledger 账目草稿")
    if "amount" in fields:
        summary = f"{summary} · {fields.get('currency', 'CNY')} {fields['amount']}"
    return {
        "protocol": "shadow.review.v1",
        "review_id": str(record.id),
        "reference": f"shadow://ledger/records/{record.id}",
        "revision": record.revision,
        "domain": "ledger",
        "intent": "ledger.record",
        "summary": summary,
        "fields": fields,
        "risk_level": "L2",
        "state": state,
        "created_at": record.created_at.isoformat(),
        "source_refs": source_refs,
        "trace_id": trace_id,
        "receipt": receipt,
        "replayed": replayed,
    }
