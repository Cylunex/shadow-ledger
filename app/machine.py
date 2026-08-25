from __future__ import annotations

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
    LedgerAgentGrant,
    LedgerRecord,
    MoneyCategory,
    MoneyEntry,
)
from app.schemas import Money, MoneyEntryInput, RecordCreate, StrictModel, jsonable
from app.services.records import confirm_record, create_record, get_record

router = APIRouter(prefix="/api/machine/v1/agent", tags=["machine-agent"])


class AgentRecordDraftCreate(StrictModel):
    occurred_at: datetime
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    money_type: Literal["expense", "income", "refund"]
    amount: Money
    currency: str = Field(min_length=3, max_length=3)
    category_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,49}$")
    title: str = Field(default="", max_length=160)

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
        )
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .outerjoin(MoneyCategory, MoneyCategory.id == MoneyEntry.category_id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == grant.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
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
    record = _owned_agent_draft(db, grant, identity, record_id)
    if record.state == "confirmed":
        return {
            "record_ref": f"shadow://ledger/records/{record.id}",
            "state": "confirmed",
            "revision": record.revision,
            "replayed": True,
            "final_entry_created": True,
        }
    if record.state != "draft":
        raise AppError(409, "invalid_state_transition", "只有草稿可以确认")

    record = confirm_record(
        db,
        grant.owner_id,
        record_id,
        body.revision,
        identity.agent_id,
        actor_type="agent",
    )
    return {
        "record_ref": f"shadow://ledger/records/{record.id}",
        "state": "confirmed",
        "revision": record.revision,
        "replayed": False,
        "final_entry_created": True,
    }


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
    rejected = db.scalar(
        select(AuditEvent.id).where(
            AuditEvent.owner_id == grant.owner_id,
            AuditEvent.aggregate_type == "record",
            AuditEvent.aggregate_id == record_id,
            AuditEvent.action == "record.draft_rejected",
            AuditEvent.actor_type == "agent",
            AuditEvent.actor_id == identity.agent_id,
        )
    )
    if rejected is not None:
        return {
            "record_ref": f"shadow://ledger/records/{record_id}",
            "state": "rejected",
            "replayed": True,
        }
    record = _owned_agent_draft(db, grant, identity, record_id)
    if record.state != "draft":
        raise AppError(409, "invalid_state_transition", "只有草稿可以退回")
    if record.revision != body.revision:
        raise AppError(409, "revision_conflict", "记录已被其他操作修改，请刷新后重试")
    db.delete(record)
    db.add(
        AuditEvent(
            owner_id=grant.owner_id,
            actor_type="agent",
            actor_id=identity.agent_id,
            action="record.draft_rejected",
            aggregate_type="record",
            aggregate_id=record_id,
        )
    )
    db.commit()
    return {
        "record_ref": f"shadow://ledger/records/{record_id}",
        "state": "rejected",
        "replayed": False,
    }
