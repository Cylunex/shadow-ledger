from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.errors import AppError
from app.models import (
    AssetBinding,
    AuditEvent,
    ConsumptionEvent,
    ConsumptionLine,
    ExternalReference,
    IdempotencyRecord,
    ItemIdentity,
    LedgerRecord,
    LedgerRecordSource,
    Merchant,
    MoneyCategory,
    MoneyEntry,
    OutboxEvent,
)
from app.payments import PAYMENT_METHOD_LABELS, PaymentMethod
from app.schemas import ConsumptionInput, MoneyEntryInput, RecordCreate, RecordPatch, jsonable


def _category(db: Session, owner_id: str, key: str | None) -> MoneyCategory | None:
    if not key:
        return None
    category = db.scalar(
        select(MoneyCategory).where(MoneyCategory.owner_id == owner_id, MoneyCategory.key == key)
    )
    if category is None:
        raise AppError(422, "category_not_found", "分类不存在", {"category_key": key})
    return category


def _money(db: Session, owner_id: str, record: LedgerRecord, data: MoneyEntryInput) -> MoneyEntry:
    category = _category(db, owner_id, data.category_key)
    if data.related_entry_id:
        related = db.scalar(
            select(MoneyEntry)
            .join(LedgerRecord)
            .where(MoneyEntry.id == data.related_entry_id, LedgerRecord.owner_id == owner_id)
        )
        if related is None or related.type != "expense":
            raise AppError(422, "invalid_related_entry", "退款只能关联本人的支出记录")
        if data.type != "refund":
            raise AppError(422, "invalid_related_entry", "只有退款可以关联原支出")
    entry = MoneyEntry(
        record=record,
        type=data.type,
        amount=data.amount,
        currency=data.currency,
        category_id=category.id if category else None,
        title=data.title,
        related_entry_id=data.related_entry_id,
        payment_method=data.payment_method,
    )
    db.add(entry)
    db.flush()
    return entry


def _consumption(
    db: Session,
    owner_id: str,
    record: LedgerRecord,
    money: MoneyEntry | None,
    data: ConsumptionInput,
) -> ConsumptionEvent:
    if (
        data.merchant_id
        and db.scalar(
            select(Merchant.id).where(
                Merchant.id == data.merchant_id, Merchant.owner_id == owner_id
            )
        )
        is None
    ):
        raise AppError(422, "merchant_not_found", "商家不存在")
    item_ids = {line.item_identity_id for line in data.lines if line.item_identity_id}
    if item_ids:
        found = set(
            db.scalars(
                select(ItemIdentity.id).where(
                    ItemIdentity.id.in_(item_ids), ItemIdentity.owner_id == owner_id
                )
            )
        )
        if found != item_ids:
            raise AppError(422, "item_not_found", "消费明细包含无效内容身份")
    event = ConsumptionEvent(
        record_id=record.id,
        money_entry_id=money.id if money else None,
        scene=data.scene,
        merchant_id=data.merchant_id,
        merchant_name_raw=data.merchant_name_raw,
        channel_key=data.channel_key,
        channel_name_raw=data.channel_name_raw,
        place_ref=data.place_ref,
        rating=data.rating,
        would_repeat=data.would_repeat,
        note=data.note,
    )
    event.lines = [
        ConsumptionLine(
            raw_name=line.raw_name,
            item_identity_id=line.item_identity_id,
            quantity=line.quantity,
            unit=line.unit,
            amount=line.amount,
            content_category=line.content_category,
            note=line.note,
            sort_order=line.sort_order,
        )
        for line in data.lines
    ]
    return event


def request_hash(data: Any) -> bytes:
    def compatible(value: Any) -> Any:
        if isinstance(value, list):
            return [compatible(item) for item in value]
        if isinstance(value, dict):
            result = {key: compatible(item) for key, item in value.items()}
            entry = result.get("money_entry")
            if isinstance(entry, dict) and entry.get("payment_method") is None:
                # 0006 must not invalidate pre-upgrade idempotency keys.
                entry.pop("payment_method", None)
            return result
        return value

    encoded = json.dumps(compatible(jsonable(data)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).digest()


def idempotency_lookup(
    db: Session, owner_id: str, operation: str, key: str | None, payload: Any
) -> IdempotencyRecord | None:
    if not key:
        raise AppError(400, "idempotency_key_required", "创建请求必须提供 Idempotency-Key")
    row = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.owner_id == owner_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    digest = request_hash(payload)
    if row and row.request_hash != digest:
        raise AppError(409, "idempotency_mismatch", "同一幂等键不能用于不同请求")
    return row


def idempotency_save(
    db: Session, owner_id: str, operation: str, key: str, payload: Any, resource_id: uuid.UUID
) -> None:
    db.add(
        IdempotencyRecord(
            owner_id=owner_id,
            operation=operation,
            idempotency_key=key,
            request_hash=request_hash(payload),
            response_status=201,
            resource_id=resource_id,
        )
    )


def create_record(
    db: Session,
    owner_id: str,
    data: RecordCreate,
    idempotency_key: str,
    actor_id: str,
    actor_type: str = "user",
    commit: bool = True,
) -> LedgerRecord:
    existing = idempotency_lookup(
        db, owner_id, "records.create", idempotency_key, data.model_dump()
    )
    if existing and existing.resource_id:
        return get_record(db, owner_id, existing.resource_id)
    record = LedgerRecord(
        owner_id=owner_id,
        record_kind="consumption" if data.consumption else "money_only",
        state="draft",
        occurred_at=data.occurred_at,
        timezone=data.timezone,
        note=data.note,
    )
    db.add(record)
    db.flush()
    money = _money(db, owner_id, record, data.money_entry) if data.money_entry else None
    if data.consumption:
        event = _consumption(db, owner_id, record, money, data.consumption)
        db.add(event)
        record.consumption = event
    db.flush()
    if data.confirm:
        _confirm_locked(db, record, actor_id)
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="record.created",
            aggregate_type="record",
            aggregate_id=record.id,
            details={"state": record.state, "record_kind": record.record_kind},
        )
    )
    idempotency_save(db, owner_id, "records.create", idempotency_key, data.model_dump(), record.id)
    if commit:
        db.commit()
        return get_record(db, owner_id, record.id)
    db.flush()
    return record


def record_query():
    return select(LedgerRecord).options(
        selectinload(LedgerRecord.money_entry),
        selectinload(LedgerRecord.consumption).selectinload(ConsumptionEvent.lines),
    )


def get_record(db: Session, owner_id: str, record_id: uuid.UUID) -> LedgerRecord:
    record = db.scalar(
        record_query().where(LedgerRecord.id == record_id, LedgerRecord.owner_id == owner_id)
    )
    if record is None:
        raise AppError(404, "record_not_found", "记录不存在")
    return record


def list_records(
    db: Session,
    owner_id: str,
    state: str | None = None,
    money_type: str | None = None,
    scene: str | None = None,
    category_key: str | None = None,
    merchant_id: uuid.UUID | None = None,
    item_identity_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    query: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    payment_method: PaymentMethod | None = None,
) -> list[LedgerRecord]:
    statement = record_query().where(LedgerRecord.owner_id == owner_id)
    statement = statement.where(LedgerRecord.state == (state or "confirmed"))
    if money_type:
        statement = statement.where(LedgerRecord.money_entry.has(MoneyEntry.type == money_type))
    if payment_method:
        statement = statement.where(
            LedgerRecord.money_entry.has(MoneyEntry.payment_method == payment_method)
        )
    if scene:
        statement = statement.where(LedgerRecord.consumption.has(ConsumptionEvent.scene == scene))
    if category_key:
        statement = statement.where(
            exists(
                select(MoneyEntry.id)
                .join(MoneyCategory, MoneyEntry.category_id == MoneyCategory.id)
                .where(
                    MoneyEntry.record_id == LedgerRecord.id,
                    MoneyCategory.owner_id == owner_id,
                    MoneyCategory.key == category_key,
                )
            )
        )
    if merchant_id:
        statement = statement.where(
            LedgerRecord.consumption.has(ConsumptionEvent.merchant_id == merchant_id)
        )
    if item_identity_id:
        statement = statement.where(
            exists(
                select(ConsumptionLine.id)
                .join(ConsumptionEvent, ConsumptionLine.event_id == ConsumptionEvent.id)
                .where(
                    ConsumptionEvent.record_id == LedgerRecord.id,
                    ConsumptionLine.item_identity_id == item_identity_id,
                )
            )
        )
    if occurred_from:
        statement = statement.where(LedgerRecord.occurred_at >= occurred_from)
    if occurred_to:
        statement = statement.where(LedgerRecord.occurred_at < occurred_to)
    if amount_min is not None:
        statement = statement.where(LedgerRecord.money_entry.has(MoneyEntry.amount >= amount_min))
    if amount_max is not None:
        statement = statement.where(LedgerRecord.money_entry.has(MoneyEntry.amount <= amount_max))
    if query:
        pattern = f"%{query[:200]}%"
        statement = statement.where(
            or_(
                LedgerRecord.note.ilike(pattern),
                exists(
                    select(MoneyEntry.id).where(
                        MoneyEntry.record_id == LedgerRecord.id,
                        or_(
                            MoneyEntry.title.ilike(pattern),
                            MoneyEntry.payment_method.ilike(pattern),
                            MoneyEntry.payment_method.in_([
                                key for key, label in PAYMENT_METHOD_LABELS.items()
                                if query in label
                            ]),
                        ),
                    )
                ),
                exists(
                    select(ConsumptionEvent.id).where(
                        ConsumptionEvent.record_id == LedgerRecord.id,
                        or_(
                            ConsumptionEvent.merchant_name_raw.ilike(pattern),
                            ConsumptionEvent.channel_name_raw.ilike(pattern),
                            ConsumptionEvent.note.ilike(pattern),
                        ),
                    )
                ),
                exists(
                    select(ConsumptionLine.id)
                    .join(ConsumptionEvent, ConsumptionLine.event_id == ConsumptionEvent.id)
                    .where(
                        ConsumptionEvent.record_id == LedgerRecord.id,
                        or_(
                            ConsumptionLine.raw_name.ilike(pattern),
                            ConsumptionLine.note.ilike(pattern),
                        ),
                    )
                ),
            )
        )
    if cursor:
        try:
            occurred, raw_id = cursor.split("|", 1)
            cursor_time, cursor_id = datetime.fromisoformat(occurred), uuid.UUID(raw_id)
        except ValueError as exc:
            raise AppError(400, "invalid_cursor", "分页游标无效") from exc
        statement = statement.where(
            or_(
                LedgerRecord.occurred_at < cursor_time,
                and_(LedgerRecord.occurred_at == cursor_time, LedgerRecord.id < cursor_id),
            )
        )
    return list(
        db.scalars(
            statement.order_by(LedgerRecord.occurred_at.desc(), LedgerRecord.id.desc()).limit(limit)
        )
    )


def _check_revision(record: LedgerRecord, revision: int) -> None:
    if record.revision != revision:
        raise AppError(
            409,
            "revision_conflict",
            "记录已被更新，请刷新后重试",
            {"expected": revision, "actual": record.revision},
        )


def parse_etag(value: str | None) -> int:
    if value is None:
        raise AppError(428, "if_match_required", "写请求必须提供 If-Match")
    try:
        return int(value.strip('W/"'))
    except ValueError as exc:
        raise AppError(400, "invalid_if_match", "If-Match 格式无效") from exc


def _validate_confirmable(record: LedgerRecord, state_message: str = "只有草稿可以确认") -> None:
    if record.state != "draft":
        raise AppError(409, "invalid_state_transition", state_message)
    if record.record_kind == "consumption" and record.consumption is None:
        raise AppError(422, "record_invariant_failed", "消费记录必须包含消费事件")
    if record.record_kind == "money_only" and record.consumption is not None:
        raise AppError(422, "record_invariant_failed", "纯金额记录不能包含消费事件")


def _confirm_locked(
    db: Session, record: LedgerRecord, actor_id: str, actor_type: str = "user"
) -> None:
    _validate_confirmable(record)
    timestamp = datetime.now(UTC)
    record.state = "confirmed"
    record.confirmed_at = timestamp
    record.updated_at = timestamp
    record.revision += 1
    db.add(
        OutboxEvent(
            event_type="ledger.record.confirmed",
            aggregate_type="record",
            aggregate_id=record.id,
            payload={"record_uri": f"shadow://ledger/records/{record.id}"},
        )
    )
    db.add(
        AuditEvent(
            owner_id=record.owner_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="record.confirmed",
            aggregate_type="record",
            aggregate_id=record.id,
        )
    )


def confirm_records(
    db: Session,
    owner_id: str,
    versions: list[tuple[uuid.UUID, int]],
    actor_id: str,
    actor_type: str = "user",
) -> list[LedgerRecord]:
    record_ids = [record_id for record_id, _ in versions]
    records = list(
        db.scalars(
            record_query()
            .where(LedgerRecord.id.in_(record_ids), LedgerRecord.owner_id == owner_id)
            .with_for_update()
        )
    )
    by_id = {record.id: record for record in records}
    if len(by_id) != len(record_ids):
        raise AppError(404, "record_not_found", "批量确认包含不存在的草稿")
    ordered = [by_id[record_id] for record_id in record_ids]
    for record, (_, revision) in zip(ordered, versions, strict=True):
        _check_revision(record, revision)
        _validate_confirmable(record, "批量确认只能包含草稿")
    for record in ordered:
        _confirm_locked(db, record, actor_id, actor_type)
    db.commit()
    return ordered


def confirm_record(
    db: Session,
    owner_id: str,
    record_id: uuid.UUID,
    revision: int,
    actor_id: str,
    actor_type: str = "user",
):
    record = db.scalar(
        record_query()
        .where(LedgerRecord.id == record_id, LedgerRecord.owner_id == owner_id)
        .with_for_update()
    )
    if record is None:
        raise AppError(404, "record_not_found", "记录不存在")
    _check_revision(record, revision)
    _confirm_locked(db, record, actor_id, actor_type)
    db.commit()
    return get_record(db, owner_id, record.id)


def void_record(db: Session, owner_id: str, record_id: uuid.UUID, revision: int, actor_id: str):
    record = db.scalar(
        record_query()
        .where(LedgerRecord.id == record_id, LedgerRecord.owner_id == owner_id)
        .with_for_update()
    )
    if record is None:
        raise AppError(404, "record_not_found", "记录不存在")
    _check_revision(record, revision)
    if record.state != "confirmed":
        raise AppError(409, "invalid_state_transition", "只有已确认记录可以撤销")
    record.state = "voided"
    record.voided_at = datetime.now(UTC)
    record.revision += 1
    db.add(
        OutboxEvent(
            event_type="ledger.record.voided",
            aggregate_type="record",
            aggregate_id=record.id,
            payload={"record_uri": f"shadow://ledger/records/{record.id}"},
        )
    )
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=actor_id,
            action="record.voided",
            aggregate_type="record",
            aggregate_id=record.id,
        )
    )
    db.commit()
    return get_record(db, owner_id, record.id)


def patch_record(
    db: Session,
    owner_id: str,
    record_id: uuid.UUID,
    revision: int,
    data: RecordPatch,
    actor_id: str,
):
    record = get_record(db, owner_id, record_id)
    _check_revision(record, revision)
    if record.state == "voided":
        raise AppError(409, "record_voided", "已撤销记录不能修改")
    if record.state == "confirmed" and data.money_entry is not None and not data.correction_reason:
        raise AppError(422, "correction_reason_required", "修改已确认金额必须填写修正原因")
    changes: list[str] = []
    for field in ("occurred_at", "timezone", "note"):
        value = getattr(data, field)
        if value is not None:
            setattr(record, field, value)
            changes.append(field)
    if data.money_entry:
        if record.money_entry is None:
            money = _money(db, owner_id, record, data.money_entry)
            if record.consumption:
                record.consumption.money_entry_id = money.id
        else:
            category = _category(db, owner_id, data.money_entry.category_key)
            entry = record.money_entry
            entry.type = data.money_entry.type
            entry.amount = data.money_entry.amount
            entry.currency = data.money_entry.currency
            entry.category_id = category.id if category else None
            entry.title = data.money_entry.title
            entry.related_entry_id = data.money_entry.related_entry_id
            if "payment_method" in data.money_entry.model_fields_set:
                entry.payment_method = data.money_entry.payment_method
        changes.append("money_entry")
    if data.consumption:
        if record.record_kind != "consumption":
            raise AppError(422, "record_kind_immutable", "纯金额记录不能改为消费记录")
        if record.consumption:
            db.delete(record.consumption)
            db.flush()
        event = _consumption(db, owner_id, record, record.money_entry, data.consumption)
        db.add(event)
        record.consumption = event
        changes.append("consumption")
    record.revision += 1
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=actor_id,
            action="record.corrected" if record.state == "confirmed" else "record.updated",
            aggregate_type="record",
            aggregate_id=record.id,
            details={"fields": changes, "reason": data.correction_reason},
        )
    )
    db.commit()
    return get_record(db, owner_id, record.id)


def add_money_entry(
    db: Session,
    owner_id: str,
    record_id: uuid.UUID,
    revision: int,
    data: MoneyEntryInput,
    actor_id: str,
):
    record = get_record(db, owner_id, record_id)
    _check_revision(record, revision)
    if (
        record.record_kind != "consumption"
        or record.money_entry is not None
        or record.state == "voided"
    ):
        raise AppError(409, "money_entry_not_allowed", "当前记录不能补充金额")
    money = _money(db, owner_id, record, data)
    assert record.consumption is not None
    record.consumption.money_entry_id = money.id
    record.revision += 1
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=actor_id,
            action="record.money_added",
            aggregate_type="record",
            aggregate_id=record.id,
        )
    )
    db.commit()
    return get_record(db, owner_id, record.id)


def delete_draft(db: Session, owner_id: str, record_id: uuid.UUID, revision: int) -> None:
    record = get_record(db, owner_id, record_id)
    _check_revision(record, revision)
    if record.state != "draft":
        raise AppError(409, "confirmed_requires_void", "已确认记录必须撤销，不能删除")
    db.delete(record)
    db.commit()


def serialize_record(db: Session, record: LedgerRecord) -> dict[str, Any]:
    entry = record.money_entry
    event = record.consumption
    category_key = None
    if entry and entry.category_id:
        category = db.get(MoneyCategory, entry.category_id)
        category_key = category.key if category else None
    sources = list(
        db.scalars(select(LedgerRecordSource).where(LedgerRecordSource.record_id == record.id))
    )
    assets = list(
        db.scalars(
            select(AssetBinding).where(
                AssetBinding.owner_id == record.owner_id,
                AssetBinding.source_type == "record",
                AssetBinding.source_id == record.id,
                AssetBinding.released_at.is_(None),
            )
        )
    )
    refs = list(
        db.scalars(
            select(ExternalReference).where(
                ExternalReference.owner_id == record.owner_id,
                ExternalReference.source_type == "record",
                ExternalReference.source_id == record.id,
            )
        )
    )
    return jsonable(
        {
            "id": record.id,
            "record_kind": record.record_kind,
            "state": record.state,
            "revision": record.revision,
            "occurred_at": record.occurred_at,
            "timezone": record.timezone,
            "note": record.note,
            "confirmed_at": record.confirmed_at,
            "voided_at": record.voided_at,
            "money_entry": None
            if entry is None
            else {
                "id": entry.id,
                "type": entry.type,
                "amount": entry.amount,
                "currency": entry.currency,
                "category_key": category_key,
                "title": entry.title,
                "related_entry_id": entry.related_entry_id,
                "payment_method": entry.payment_method,
            },
            "consumption": None
            if event is None
            else {
                "id": event.id,
                "scene": event.scene,
                "merchant_id": event.merchant_id,
                "merchant_name_raw": event.merchant_name_raw,
                "channel_key": event.channel_key,
                "channel_name_raw": event.channel_name_raw,
                "place_ref": event.place_ref,
                "rating": event.rating,
                "would_repeat": event.would_repeat,
                "note": event.note,
                "lines": [
                    {
                        "id": line.id,
                        "item_identity_id": line.item_identity_id,
                        "raw_name": line.raw_name,
                        "quantity": line.quantity,
                        "unit": line.unit,
                        "amount": line.amount,
                        "content_category": line.content_category,
                        "note": line.note,
                        "sort_order": line.sort_order,
                    }
                    for line in event.lines
                ],
            },
            "sources": [{"source_id": row.source_id, "role": row.role} for row in sources],
            "assets": [
                {"id": row.id, "asset_id": row.asset_id, "usage": row.usage} for row in assets
            ],
            "external_references": [
                {"id": row.id, "relation": row.relation, "target_uri": row.target_uri}
                for row in refs
            ],
        }
    )
