from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoneyEntryInput(StrictModel):
    type: Literal["expense", "income", "refund"]
    amount: Money
    currency: str = Field(min_length=3, max_length=3)
    category_key: str | None = None
    title: str = Field(default="", max_length=500)
    related_entry_id: uuid.UUID | None = None

    @field_validator("currency")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must contain letters")
        return value.upper()


class ConsumptionLineInput(StrictModel):
    raw_name: str = Field(min_length=1, max_length=1000)
    item_identity_id: uuid.UUID | None = None
    quantity: Quantity | None = None
    unit: str | None = Field(default=None, max_length=30)
    amount: Decimal | None = Field(default=None, max_digits=18, decimal_places=4)
    content_category: str | None = Field(default=None, max_length=100)
    note: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=0, ge=0)


class ConsumptionInput(StrictModel):
    scene: Literal[
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
    ]
    merchant_id: uuid.UUID | None = None
    merchant_name_raw: str | None = Field(default=None, max_length=1000)
    channel_key: str | None = Field(default=None, max_length=50)
    channel_name_raw: str | None = Field(default=None, max_length=500)
    place_ref: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    would_repeat: bool | None = None
    note: str = Field(default="", max_length=2000)
    lines: list[ConsumptionLineInput] = Field(default_factory=list, max_length=100)

    @field_validator("place_ref")
    @classmethod
    def place_uri(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("shadow://travel/"):
            raise ValueError("place_ref must be a shadow://travel/ URI")
        return value


class RecordCreate(StrictModel):
    occurred_at: datetime
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    note: str = Field(default="", max_length=2000)
    money_entry: MoneyEntryInput | None = None
    consumption: ConsumptionInput | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def has_fact(self) -> RecordCreate:
        if self.money_entry is None and self.consumption is None:
            raise ValueError("money_entry or consumption is required")
        return self


class RecordPatch(StrictModel):
    occurred_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    money_entry: MoneyEntryInput | None = None
    consumption: ConsumptionInput | None = None
    correction_reason: str | None = Field(default=None, max_length=1000)


class TextCaptureCreate(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    external_id: str | None = Field(default=None, max_length=500)


class CategoryCreate(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,49}$")
    name: str = Field(min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=30)
    icon: str | None = Field(default=None, max_length=50)
    sort_order: int = 0


class CategoryPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=30)
    icon: str | None = Field(default=None, max_length=50)
    sort_order: int | None = None
    active: bool | None = None


class MerchantCreate(StrictModel):
    canonical_name: str = Field(min_length=1, max_length=500)
    merchant_type: str | None = Field(default=None, max_length=50)
    place_ref: str | None = None


class ItemCreate(StrictModel):
    kind: Literal[
        "product", "dish", "drink", "service", "subscription", "ticket", "digital_good", "other"
    ]
    canonical_name: str = Field(min_length=1, max_length=500)
    brand: str | None = Field(default=None, max_length=200)
    variant: str | None = Field(default=None, max_length=200)
    merchant_id: uuid.UUID | None = None
    barcode: str | None = Field(default=None, max_length=100)
    external_ids: dict[str, str] = Field(default_factory=dict)


class AliasCreate(StrictModel):
    alias: str = Field(min_length=1, max_length=500)
    source: str = Field(default="user", max_length=30)


class MergeCreate(StrictModel):
    target_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=1000)


class IntentCreate(StrictModel):
    intent_type: Literal[
        "buy", "eat", "drink", "visit", "subscribe", "renew", "cancel", "replace", "other"
    ]
    title: str = Field(min_length=1, max_length=500)
    item_identity_id: uuid.UUID | None = None
    merchant_id: uuid.UUID | None = None
    place_ref: str | None = None
    expected_amount: Money | None = None
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    desired_start: datetime | None = None
    desired_end: datetime | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    state: Literal["inbox", "considering", "planned", "due", "skipped", "cancelled"] = "inbox"
    reason: str | None = Field(default=None, max_length=2000)


class CommitmentCreate(StrictModel):
    kind: Literal[
        "subscription",
        "membership",
        "regular_service",
        "regular_purchase",
        "fixed_expense",
        "regular_income",
    ]
    title: str = Field(min_length=1, max_length=500)
    merchant_id: uuid.UUID | None = None
    item_identity_id: uuid.UUID | None = None
    expected_amount: Money | None = None
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    recurrence_rule: str = Field(min_length=1, max_length=500)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    next_due_at: datetime
    auto_renew: bool | None = None
    remind_before_seconds: int = Field(default=259200, ge=0, le=31536000)


class BudgetCreate(StrictModel):
    category_id: uuid.UUID | None = None
    budget_month: date
    monthly_amount: Money
    currency: str = Field(default="CNY", min_length=3, max_length=3)

    @field_validator("budget_month")
    @classmethod
    def month_start(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("budget_month must be the first day of a month")
        return value


class ReferenceCreate(StrictModel):
    relation: str = Field(min_length=1, max_length=50)
    target_uri: str

    @field_validator("target_uri")
    @classmethod
    def supported_uri(cls, value: str) -> str:
        allowed = ("shadow://health/", "shadow://travel/", "shadow://foliant/", "shadow://archive/")
        if not value.startswith(allowed):
            raise ValueError("unsupported shadow URI")
        return value


class ImportSourceInput(StrictModel):
    format: Literal["json", "csv", "markdown"]
    content: str = Field(max_length=1_000_000)


class ImportPreview(ImportSourceInput):
    pass


class ImportCommit(StrictModel):
    records: list[RecordCreate] = Field(default_factory=list, max_length=1000)
    source: ImportSourceInput | None = None
    external_id: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def has_one_input(self) -> ImportCommit:
        if bool(self.records) == bool(self.source):
            raise ValueError("provide either records or source")
        return self


class AssetInit(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/jpeg", "image/png", "image/webp", "application/pdf"]
    size: int = Field(gt=0, le=20_000_000)


class AssetComplete(StrictModel):
    upload_id: str = Field(min_length=1, max_length=500)
    usage: str = Field(default="evidence", max_length=50)


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, ".4f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value
