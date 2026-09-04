from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.machine import AgentRecordDraftCreate
from app.payments import PaymentMethod
from app.schemas import Money, StrictModel

Skill = Literal["research", "capture", "steward"]
Disclosure = Literal["remote_minimal", "local_private"]


class OverviewSpec(StrictModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


class SearchSpec(OverviewSpec):
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)
    merchant_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    money_type: Literal["expense", "income", "refund"] | None = None
    payment_method: PaymentMethod | None = None
    scene: str | None = Field(default=None, max_length=40)


class EntitySpec(OverviewSpec):
    kind: Literal["merchant", "item"]
    entity_id: uuid.UUID


class AttentionSpec(StrictModel):
    kind: Literal["forecast", "recurring", "draft", "unknown_amount", "identity_merge"] = "forecast"
    limit: int = Field(default=20, ge=1, le=50)


class Claim(StrictModel):
    metric_id: str = Field(max_length=100)
    value: str = Field(max_length=100)
    currency: str | None = None
    status: Literal["exact", "partial", "projected"]


class ExplainSpec(StrictModel):
    query_id: uuid.UUID
    claims: list[Claim] = Field(default_factory=list, max_length=50)


class ParseSpec(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    occurred_at: datetime | None = None
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")

    @field_validator("occurred_at")
    @classmethod
    def offset(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("occurred_at requires UTC offset")
        return value


class DraftSpec(AgentRecordDraftCreate):
    idempotency_key: str = Field(min_length=8, max_length=128)
    source_text: str | None = Field(default=None, max_length=4000)


class ReviseSpec(StrictModel):
    record_id: uuid.UUID
    revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    amount: Money | None = None
    title: str | None = Field(default=None, max_length=160)
    payment_method: PaymentMethod | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_input(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("amount must be a decimal string")
        return value


class AttachSpec(StrictModel):
    record_id: uuid.UUID
    revision: int = Field(ge=1)
    source_id: uuid.UUID
    idempotency_key: str = Field(min_length=8, max_length=128)


class ToolCall(StrictModel):
    skill: Skill
    catalog_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool: str = Field(max_length=64)
    arguments: dict = Field(default_factory=dict)


class ReviewRequest(StrictModel):
    record_id: uuid.UUID
    revision: int = Field(ge=1)
    action: Literal["confirm", "reject"] = "confirm"


class ApprovalDecision(StrictModel):
    display_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    accept: bool


class ExecuteRequest(StrictModel):
    approval_grant_id: uuid.UUID
