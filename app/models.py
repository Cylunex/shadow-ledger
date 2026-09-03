from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def now() -> datetime:
    return datetime.now(UTC)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class LedgerRecord(Timestamps, Base):
    __tablename__ = "ledger_records"
    __table_args__ = (
        CheckConstraint("record_kind IN ('money_only','consumption')", name="ck_record_kind"),
        CheckConstraint("state IN ('draft','confirmed','voided')", name="ck_record_state"),
        CheckConstraint("revision > 0", name="ck_record_revision"),
        CheckConstraint(
            "(state='draft' AND confirmed_at IS NULL AND voided_at IS NULL) OR "
            "(state='confirmed' AND confirmed_at IS NOT NULL AND voided_at IS NULL) OR "
            "(state='voided' AND confirmed_at IS NOT NULL AND voided_at IS NOT NULL)",
            name="ck_record_state_times",
        ),
        Index("idx_records_owner_time", "owner_id", "occurred_at", "id"),
        Index("idx_records_owner_state", "owner_id", "state", "updated_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    record_kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(12), default="draft")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    note: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    money_entry: Mapped[MoneyEntry | None] = relationship(
        back_populates="record", cascade="all, delete-orphan", uselist=False
    )
    consumption: Mapped[ConsumptionEvent | None] = relationship(
        back_populates="record",
        cascade="all, delete-orphan",
        uselist=False,
        foreign_keys="ConsumptionEvent.record_id",
    )


class MoneyCategory(Timestamps, Base):
    __tablename__ = "money_categories"
    __table_args__ = (UniqueConstraint("owner_id", "key"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(30))
    icon: Mapped[str | None] = mapped_column(String(50))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MoneyEntry(Timestamps, Base):
    __tablename__ = "money_entries"
    __table_args__ = (
        CheckConstraint("type IN ('expense','income','refund')", name="ck_money_type"),
        CheckConstraint("amount > 0", name="ck_money_amount"),
        CheckConstraint(
            "payment_method IS NULL OR payment_method IN "
            "('alipay','wechat','jd_pay','jd_baitiao','huabei','gift_card','cash',"
            "'bank_card','bank_transfer','mixed','other')",
            name="ck_money_payment_method",
        ),
        CheckConstraint(
            "related_entry_id IS NULL OR related_entry_id <> id", name="ck_money_related"
        ),
        UniqueConstraint("id", "record_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_records.id"), unique=True)
    type: Mapped[str] = mapped_column(String(12))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("money_categories.id"))
    title: Mapped[str] = mapped_column(Text, default="")
    related_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("money_entries.id"))
    payment_method: Mapped[str | None] = mapped_column(String(24), nullable=True)
    record: Mapped[LedgerRecord] = relationship(back_populates="money_entry")


class Merchant(Timestamps, Base):
    __tablename__ = "merchants"
    __table_args__ = (
        CheckConstraint(
            "canonical_merchant_id IS NULL OR canonical_merchant_id <> id",
            name="ck_merchant_redirect",
        ),
        Index("idx_merchants_owner_name", "owner_id", "canonical_name"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    canonical_name: Mapped[str] = mapped_column(Text)
    merchant_type: Mapped[str | None] = mapped_column(String(50))
    place_ref: Mapped[str | None] = mapped_column(Text)
    canonical_merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MerchantAlias(Base):
    __tablename__ = "merchant_aliases"
    __table_args__ = (UniqueConstraint("merchant_id", "normalized_alias"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    alias: Mapped[str] = mapped_column(Text)
    normalized_alias: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ItemIdentity(Timestamps, Base):
    __tablename__ = "item_identities"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('product','dish','drink','service','subscription','ticket','digital_good','other')",
            name="ck_item_kind",
        ),
        CheckConstraint(
            "canonical_item_id IS NULL OR canonical_item_id <> id", name="ck_item_redirect"
        ),
        Index("idx_items_owner_name", "owner_id", "canonical_name"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30))
    canonical_name: Mapped[str] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    variant: Mapped[str | None] = mapped_column(Text)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    barcode: Mapped[str | None] = mapped_column(String(100))
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    canonical_item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item_identities.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ItemAlias(Base):
    __tablename__ = "item_aliases"
    __table_args__ = (UniqueConstraint("item_identity_id", "normalized_alias"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    item_identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item_identities.id"))
    alias: Mapped[str] = mapped_column(Text)
    normalized_alias: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ConsumptionEvent(Timestamps, Base):
    __tablename__ = "consumption_events"
    __table_args__ = (
        CheckConstraint(
            "scene IN ('online_purchase','offline_purchase','delivery','dine_in','drink','service',"
            "'subscription','transport','entertainment','travel','other')",
            name="ck_consumption_scene",
        ),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_consumption_rating"),
        ForeignKeyConstraint(
            ["money_entry_id", "record_id"], ["money_entries.id", "money_entries.record_id"]
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_records.id"), unique=True)
    money_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    scene: Mapped[str] = mapped_column(String(30))
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    merchant_name_raw: Mapped[str | None] = mapped_column(Text)
    channel_key: Mapped[str | None] = mapped_column(String(50))
    channel_name_raw: Mapped[str | None] = mapped_column(Text)
    place_ref: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    would_repeat: Mapped[bool | None] = mapped_column(Boolean)
    note: Mapped[str] = mapped_column(Text, default="")
    record: Mapped[LedgerRecord] = relationship(
        back_populates="consumption", foreign_keys=[record_id]
    )
    lines: Mapped[list[ConsumptionLine]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="ConsumptionLine.sort_order"
    )


class ConsumptionLine(Timestamps, Base):
    __tablename__ = "consumption_lines"
    __table_args__ = (
        CheckConstraint("quantity IS NULL OR quantity > 0", name="ck_line_quantity"),
        Index("idx_lines_event", "event_id", "sort_order", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("consumption_events.id"))
    item_identity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item_identities.id"))
    raw_name: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(30))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    content_category: Mapped[str | None] = mapped_column(String(50))
    note: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    event: Mapped[ConsumptionEvent] = relationship(back_populates="lines")


class CaptureSource(Base):
    __tablename__ = "capture_sources"
    __table_args__ = (
        CheckConstraint(
            "capture_state IN ('received','processing','parsed','failed')", name="ck_capture_state"
        ),
        Index(
            "uq_capture_external",
            "owner_id",
            "source_type",
            "source_external_id",
            unique=True,
            postgresql_where="source_external_id IS NOT NULL",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    source_external_id: Mapped[str | None] = mapped_column(Text)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    parser: Mapped[str | None] = mapped_column(String(80))
    parser_version: Mapped[str | None] = mapped_column(String(40))
    capture_state: Mapped[str] = mapped_column(String(20), default="received")
    error_code: Mapped[str | None] = mapped_column(String(80))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LedgerRecordSource(Base):
    __tablename__ = "ledger_record_sources"
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_records.id"), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_sources.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), primary_key=True, default="evidence")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RecurringCommitment(Timestamps, Base):
    __tablename__ = "recurring_commitments"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('subscription','membership','regular_service','regular_purchase','fixed_expense','regular_income')",
            name="ck_commitment_kind",
        ),
        CheckConstraint("state IN ('active','paused','ended')", name="ck_commitment_state"),
        CheckConstraint(
            "expected_amount IS NULL OR expected_amount > 0", name="ck_commitment_amount"
        ),
        Index("idx_recurring_due", "owner_id", "state", "next_due_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(Text)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    item_identity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item_identities.id"))
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    recurrence_rule: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64))
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    auto_renew: Mapped[bool | None] = mapped_column(Boolean)
    remind_before_seconds: Mapped[int] = mapped_column(Integer, default=259200)
    state: Mapped[str] = mapped_column(String(20), default="active")
    last_record_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_records.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class SpendingIntent(Timestamps, Base):
    __tablename__ = "spending_intents"
    __table_args__ = (
        CheckConstraint(
            "intent_type IN ('buy','eat','drink','visit','subscribe','renew','cancel','replace','other')",
            name="ck_intent_type",
        ),
        CheckConstraint(
            "state IN ('inbox','considering','planned','due','completed','skipped','cancelled')",
            name="ck_intent_state",
        ),
        CheckConstraint("priority IN ('low','normal','high')", name="ck_intent_priority"),
        CheckConstraint(
            "desired_end IS NULL OR desired_start IS NULL OR desired_end >= desired_start",
            name="ck_intent_dates",
        ),
        Index("idx_intents_owner_state", "owner_id", "state", "desired_start"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    intent_type: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(Text)
    item_identity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("item_identities.id"))
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    place_ref: Mapped[str | None] = mapped_column(Text)
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    desired_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    desired_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    state: Mapped[str] = mapped_column(String(20), default="inbox")
    reason: Mapped[str | None] = mapped_column(Text)
    completed_record_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_records.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class BudgetTarget(Timestamps, Base):
    __tablename__ = "budget_targets"
    __table_args__ = (
        CheckConstraint("monthly_amount > 0", name="ck_budget_amount"),
        CheckConstraint("revision > 0", name="ck_budget_revision"),
        Index(
            "uq_budget_total_active",
            "owner_id",
            "currency",
            "budget_month",
            unique=True,
            postgresql_where="category_id IS NULL AND active",
        ),
        Index(
            "uq_budget_category_active",
            "owner_id",
            "currency",
            "budget_month",
            "category_id",
            unique=True,
            postgresql_where="category_id IS NOT NULL AND active",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("money_categories.id"))
    budget_month: Mapped[date] = mapped_column(Date)
    monthly_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Reminder(Timestamps, Base):
    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint("owner_id", "reminder_key"),
        CheckConstraint(
            "state IN ('pending','read','handled','dismissed')", name="ck_reminder_state"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    reminder_key: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ExternalReference(Base):
    __tablename__ = "external_references"
    __table_args__ = (UniqueConstraint("source_type", "source_id", "relation", "target_uri"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    relation: Mapped[str] = mapped_column(String(50))
    target_uri: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssetBinding(Base):
    __tablename__ = "asset_bindings"
    __table_args__ = (UniqueConstraint("source_type", "source_id", "asset_id", "usage"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    asset_reference_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    usage: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("idx_outbox_pending", "available_at", "id"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(100))
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BackgroundJob(Timestamps, Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint("state IN ('pending','running','succeeded','failed')", name="ck_job_state"),
        Index("idx_jobs_ready", "available_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error_code: Mapped[str | None] = mapped_column(String(80))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("idx_audit_aggregate", "aggregate_type", "aggregate_id", "occurred_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(80))
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LocalIdentity(Base):
    __tablename__ = "local_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("local_identities.id"))
    session_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    groups_snapshot: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OidcTransaction(Base):
    __tablename__ = "oidc_transactions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    state_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    browser_binding_hash: Mapped[bytes] = mapped_column(LargeBinary)
    nonce_hash: Mapped[bytes] = mapped_column(LargeBinary)
    pkce_verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    redirect_uri: Mapped[str] = mapped_column(Text)
    return_to: Mapped[str] = mapped_column(Text, default="/")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_id", "operation", "idempotency_key"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[bytes] = mapped_column(LargeBinary)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: now() + timedelta(days=7)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LedgerAgentGrant(Timestamps, Base):
    __tablename__ = "ledger_agent_grants"
    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_ledger_agent_grant_agent"),
        Index("idx_ledger_agent_grants_owner", "owner_id", "active"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[str] = mapped_column(Text)
    allow_summary: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_records: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_budgets: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_drafts: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    owner_id: Mapped[str] = mapped_column(Text, primary_key=True)
    default_currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(64))
    locale: Mapped[str] = mapped_column(String(20), default="zh-CN")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class IdentitySuggestion(Timestamps, Base):
    __tablename__ = "identity_suggestions"
    __table_args__ = (UniqueConstraint("owner_id", "suggestion_key"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    suggestion_key: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="pending")


class ImportBatch(Timestamps, Base):
    """One user-visible import/review run, not an account reconciliation."""

    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_import_batch_idempotency"),
        CheckConstraint("state IN ('open','completed')", name="ck_import_batch_state"),
        Index("idx_import_batches_owner_created", "owner_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[bytes] = mapped_column(LargeBinary)
    platform: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="open")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)


class ImportReviewItem(Timestamps, Base):
    """Review metadata kept beside immutable imported source text."""

    __tablename__ = "import_review_items"
    __table_args__ = (
        CheckConstraint(
            "review_state IN ('pending','resolved','dismissed')",
            name="ck_import_review_state",
        ),
        CheckConstraint("revision > 0", name="ck_import_review_revision"),
        Index("idx_import_review_owner_state", "owner_id", "review_state", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_batches.id"))
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capture_sources.id"))
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_records.id"))
    source_external_id: Mapped[str] = mapped_column(Text)
    raw_merchant_name: Mapped[str | None] = mapped_column(Text)
    raw_item_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    normalized_merchant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merchants.id"))
    duplicate_of_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ledger_records.id")
    )
    refund_candidate_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("money_entries.id")
    )
    amount_anomaly_reason: Mapped[str | None] = mapped_column(String(120))
    review_state: Mapped[str] = mapped_column(String(20), default="pending")
    resolution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class MerchantNormalizationRule(Timestamps, Base):
    """Explainable exact-match rule learned only from a user's confirmation."""

    __tablename__ = "merchant_normalization_rules"
    __table_args__ = (
        CheckConstraint("match_kind = 'raw_merchant_exact'", name="ck_merchant_rule_kind"),
        CheckConstraint("revision > 0", name="ck_merchant_rule_revision"),
        CheckConstraint("evidence_count > 0", name="ck_merchant_rule_evidence"),
        Index("idx_merchant_rules_lookup", "owner_id", "normalized_value", "active"),
        Index(
            "uq_merchant_rules_active",
            "owner_id",
            "normalized_value",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    match_kind: Mapped[str] = mapped_column(String(40), default="raw_merchant_exact")
    normalized_value: Mapped[str] = mapped_column(Text)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    explanation: Mapped[str] = mapped_column(Text)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    source_review_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_review_items.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveEvidenceLink(Timestamps, Base):
    """Stable linkage only; Ledger never copies Asset bytes into Archive."""

    __tablename__ = "archive_evidence_links"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "record_id",
            "asset_binding_id",
            "archive_uri",
            name="uq_archive_evidence_link",
        ),
        CheckConstraint("revision > 0", name="ck_archive_evidence_revision"),
        Index("idx_archive_evidence_record", "owner_id", "record_id", "active"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_records.id"))
    asset_binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset_bindings.id"))
    archive_uri: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UseCycle(Timestamps, Base):
    """A user-declared period of actually using an item, never inferred from purchase alone."""

    __tablename__ = "use_cycles"
    __table_args__ = (
        CheckConstraint("state IN ('active','completed','cancelled')", name="ck_use_cycle_state"),
        CheckConstraint("revision > 0", name="ck_use_cycle_revision"),
        CheckConstraint(
            "expected_end_at IS NULL OR expected_end_at >= started_at",
            name="ck_use_cycle_expected_end",
        ),
        CheckConstraint(
            "(state='active' AND ended_at IS NULL) OR "
            "(state IN ('completed','cancelled') AND ended_at IS NOT NULL AND ended_at >= started_at)",
            name="ck_use_cycle_state_time",
        ),
        Index("idx_use_cycles_owner_state", "owner_id", "state", "started_at"),
        Index("idx_use_cycles_item", "owner_id", "item_identity_id", "started_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    item_identity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("item_identities.id"))
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_records.id"))
    label: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expected_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="active")
    note: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ForecastRun(Base):
    """Immutable, replayable input snapshot for one deterministic forecast calculation."""

    __tablename__ = "forecast_runs"
    __table_args__ = (
        CheckConstraint("horizon_days BETWEEN 1 AND 365", name="ck_forecast_horizon"),
        UniqueConstraint(
            "owner_id", "algorithm_version", "input_hash", name="uq_forecast_run_input"
        ),
        Index("idx_forecast_runs_owner_created", "owner_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(Text)
    as_of: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    horizon_days: Mapped[int] = mapped_column(Integer)
    algorithm_version: Mapped[str] = mapped_column(String(40))
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    input_hash: Mapped[bytes] = mapped_column(LargeBinary)
    output_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ForecastItem(Timestamps, Base):
    """A derived suggestion. It never participates in confirmed Ledger summaries."""

    __tablename__ = "forecast_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('commitment_due','repeat_purchase','use_cycle_end')",
            name="ck_forecast_item_kind",
        ),
        CheckConstraint("state IN ('active','dismissed')", name="ck_forecast_item_state"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_forecast_confidence"),
        CheckConstraint("revision > 0", name="ck_forecast_item_revision"),
        CheckConstraint(
            "expected_amount IS NULL OR (expected_amount > 0 AND currency IS NOT NULL)",
            name="ck_forecast_item_amount",
        ),
        UniqueConstraint("run_id", "source_key", name="uq_forecast_item_source"),
        Index("idx_forecast_items_run_time", "run_id", "predicted_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("forecast_runs.id"))
    source_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30))
    target_uri: Mapped[str] = mapped_column(Text)
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    explanation: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
