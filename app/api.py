from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.integrations import AssetClient
from app.models import (
    AssetBinding,
    AuditEvent,
    BackgroundJob,
    BudgetTarget,
    CaptureSource,
    ConsumptionEvent,
    ConsumptionLine,
    ExternalReference,
    IdentitySuggestion,
    ItemAlias,
    ItemIdentity,
    LedgerRecord,
    LedgerRecordSource,
    Merchant,
    MerchantAlias,
    MoneyCategory,
    MoneyEntry,
    OutboxEvent,
    RecurringCommitment,
    Reminder,
    SpendingIntent,
)
from app.schemas import (
    AliasCreate,
    AssetComplete,
    AssetInit,
    BudgetCreate,
    CategoryCreate,
    CategoryPatch,
    CommitmentCreate,
    ImportCommit,
    ImportPreview,
    IntentCreate,
    ItemCreate,
    MerchantCreate,
    MergeCreate,
    MoneyEntryInput,
    RecordCreate,
    RecordPatch,
    ReferenceCreate,
    TextCaptureCreate,
    jsonable,
)
from app.security import Actor, current_actor, require_scope
from app.services.records import (
    add_money_entry,
    confirm_record,
    create_record,
    delete_draft,
    get_record,
    idempotency_lookup,
    idempotency_save,
    list_records,
    parse_etag,
    patch_record,
    serialize_record,
    void_record,
)

router = APIRouter(prefix="/api/v1")


def etag(response: Response, revision: int) -> None:
    response.headers["ETag"] = f'"{revision}"'


def actor_id(actor: Actor) -> str:
    return actor.owner_id if actor.actor_type == "user" else "service"


def owned(db: Session, model, resource_id: uuid.UUID, owner_id: str, name: str):
    row = db.scalar(select(model).where(model.id == resource_id, model.owner_id == owner_id))
    if row is None:
        raise AppError(404, f"{name}_not_found", "资源不存在")
    return row


def simple_model(row, fields: tuple[str, ...]) -> dict[str, Any]:
    return jsonable({field: getattr(row, field) for field in fields})


def replayed_create(
    db: Session,
    actor: Actor,
    operation: str,
    key: str | None,
    payload: Any,
    model,
    name: str,
):
    replay = idempotency_lookup(db, actor.owner_id, operation, key, jsonable(payload))
    if replay and replay.resource_id:
        return owned(db, model, replay.resource_id, actor.owner_id, name)
    return None


def remember_create(
    db: Session,
    actor: Actor,
    operation: str,
    key: str,
    payload: Any,
    resource_id: uuid.UUID,
) -> None:
    idempotency_save(db, actor.owner_id, operation, key, jsonable(payload), resource_id)


@router.get("/me")
def me(actor: Actor = Depends(current_actor), settings: Settings = Depends(get_settings)):
    return {
        "owner_id": actor.owner_id,
        "default_currency": settings.default_currency,
        "timezone": settings.default_timezone,
        "capabilities": sorted(actor.scopes),
    }


@router.post("/records", status_code=201)
def records_create(
    data: RecordCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.write-draft")),
    db: Session = Depends(get_db),
):
    if data.confirm and "ledger.confirm" not in actor.scopes:
        raise AppError(403, "confirmation_scope_required", "当前客户端不能确认正式事实")
    record = create_record(db, actor.owner_id, data, idempotency_key or "", actor_id(actor))
    etag(response, record.revision)
    return serialize_record(db, record)


@router.get("/records")
def records_list(
    state: str | None = None,
    money_type: str | None = None,
    scene: str | None = None,
    category_key: str | None = None,
    merchant_id: uuid.UUID | None = None,
    item_identity_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    amount_min: Decimal | None = Query(default=None, ge=0),
    amount_max: Decimal | None = Query(default=None, ge=0),
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    actor: Actor = Depends(require_scope("ledger.read")),
    db: Session = Depends(get_db),
):
    rows = list_records(
        db,
        actor.owner_id,
        state,
        money_type,
        scene,
        category_key,
        merchant_id,
        item_identity_id,
        occurred_from,
        occurred_to,
        amount_min,
        amount_max,
        query,
        limit + 1,
        cursor,
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = f"{last.occurred_at.isoformat()}|{last.id}"
        rows = rows[:limit]
    return {"items": [serialize_record(db, row) for row in rows], "next_cursor": next_cursor}


@router.get("/records/{record_id}")
def records_get(
    record_id: uuid.UUID,
    response: Response,
    actor: Actor = Depends(require_scope("ledger.read")),
    db: Session = Depends(get_db),
):
    record = get_record(db, actor.owner_id, record_id)
    etag(response, record.revision)
    return serialize_record(db, record)


@router.patch("/records/{record_id}")
def records_patch(
    record_id: uuid.UUID,
    data: RecordPatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.write-draft")),
    db: Session = Depends(get_db),
):
    record = patch_record(
        db, actor.owner_id, record_id, parse_etag(if_match), data, actor_id(actor)
    )
    etag(response, record.revision)
    return serialize_record(db, record)


@router.post("/records/{record_id}/confirm")
def records_confirm(
    record_id: uuid.UUID,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.confirm")),
    db: Session = Depends(get_db),
):
    record = confirm_record(db, actor.owner_id, record_id, parse_etag(if_match), actor_id(actor))
    etag(response, record.revision)
    return serialize_record(db, record)


@router.post("/records/{record_id}/void")
def records_void(
    record_id: uuid.UUID,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.confirm")),
    db: Session = Depends(get_db),
):
    record = void_record(db, actor.owner_id, record_id, parse_etag(if_match), actor_id(actor))
    etag(response, record.revision)
    return serialize_record(db, record)


@router.post("/records/{record_id}/money-entry")
def records_money(
    record_id: uuid.UUID,
    data: MoneyEntryInput,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.write-draft")),
    db: Session = Depends(get_db),
):
    record = add_money_entry(
        db, actor.owner_id, record_id, parse_etag(if_match), data, actor_id(actor)
    )
    etag(response, record.revision)
    return serialize_record(db, record)


@router.delete("/records/{record_id}", status_code=204)
def records_delete(
    record_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.write-draft")),
    db: Session = Depends(get_db),
):
    delete_draft(db, actor.owner_id, record_id, parse_etag(if_match))


DEFAULT_CATEGORIES = (
    ("food", "餐饮"),
    ("shopping", "购物"),
    ("transport", "交通"),
    ("housing", "居住"),
    ("services", "生活服务"),
    ("entertainment", "娱乐"),
    ("travel", "旅行"),
    ("health", "健康"),
    ("subscription", "订阅"),
    ("other", "其他"),
)


def ensure_categories(db: Session, owner_id: str) -> None:
    if db.scalar(
        select(func.count()).select_from(MoneyCategory).where(MoneyCategory.owner_id == owner_id)
    ):
        return
    for order, (key, name) in enumerate(DEFAULT_CATEGORIES):
        db.add(MoneyCategory(owner_id=owner_id, key=key, name=name, sort_order=order))
    db.commit()


@router.get("/categories")
def categories_list(actor: Actor = Depends(current_actor), db: Session = Depends(get_db)):
    ensure_categories(db, actor.owner_id)
    rows = db.scalars(
        select(MoneyCategory)
        .where(MoneyCategory.owner_id == actor.owner_id)
        .order_by(MoneyCategory.sort_order)
    )
    return {
        "items": [
            simple_model(row, ("id", "key", "name", "color", "icon", "sort_order", "active"))
            for row in rows
        ]
    }


@router.post("/categories", status_code=201)
def categories_create(
    data: CategoryCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    replay = replayed_create(
        db,
        actor,
        "categories.create",
        idempotency_key,
        data.model_dump(),
        MoneyCategory,
        "category",
    )
    if replay:
        return simple_model(replay, ("id", "key", "name", "color", "icon", "sort_order", "active"))
    row = MoneyCategory(owner_id=actor.owner_id, **data.model_dump())
    db.add(row)
    try:
        db.flush()
        remember_create(
            db, actor, "categories.create", idempotency_key or "", data.model_dump(), row.id
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(409, "category_key_exists", "分类 key 已存在") from exc
    return simple_model(row, ("id", "key", "name", "color", "icon", "sort_order", "active"))


@router.patch("/categories/{category_id}")
def categories_patch(
    category_id: uuid.UUID,
    data: CategoryPatch,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, MoneyCategory, category_id, actor.owner_id, "category")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    return simple_model(row, ("id", "key", "name", "color", "icon", "sort_order", "active"))


AMOUNT_RE = re.compile(r"(?:(?:金额|花了|消费|收入|退款)[：:\s¥￥]*)?(\d+(?:\.\d{1,4})?)")


def parse_text(text: str, settings: Settings) -> tuple[RecordCreate, dict[str, Any]]:
    amount_match = AMOUNT_RE.search(text)
    money_type = (
        "refund"
        if "退款" in text or "退了" in text
        else "income"
        if "收入" in text or "工资" in text
        else "expense"
    )
    scene = None
    for token, value in (
        ("外卖", "delivery"),
        ("堂食", "dine_in"),
        ("咖啡", "drink"),
        ("订阅", "subscription"),
        ("打车", "transport"),
        ("网购", "online_purchase"),
    ):
        if token in text:
            scene = value
            break
    occurred = datetime.now(UTC)
    if "昨天" in text:
        occurred -= timedelta(days=1)
    elif "前天" in text:
        occurred -= timedelta(days=2)
    money = None
    if amount_match:
        money = MoneyEntryInput(
            type=money_type,
            amount=Decimal(amount_match.group(1)),
            currency=settings.default_currency,
            title=text[:500],
        )
    consumption = None
    if scene:
        consumption = {"scene": scene, "lines": [], "note": ""}
    candidate = RecordCreate(
        occurred_at=occurred,
        timezone=settings.default_timezone,
        money_entry=money,
        consumption=consumption,
        note="",
        confirm=False,
    )
    fields = {
        "amount": {
            "value": amount_match.group(1) if amount_match else None,
            "confidence": 0.98 if amount_match else 0.0,
        },
        "type": {"value": money_type, "confidence": 0.95},
        "scene": {"value": scene, "confidence": 0.9 if scene else 0.0},
    }
    return candidate, fields


@router.post("/capture/text", status_code=201)
def capture_text(
    data: TextCaptureCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.capture")),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    existing = idempotency_lookup(
        db, actor.owner_id, "capture.text", idempotency_key, data.model_dump()
    )
    if existing and existing.resource_id:
        record = get_record(db, actor.owner_id, existing.resource_id)
        return serialize_record(db, record)
    if data.external_id:
        source = db.scalar(
            select(CaptureSource).where(
                CaptureSource.owner_id == actor.owner_id,
                CaptureSource.source_type == "text",
                CaptureSource.source_external_id == data.external_id,
            )
        )
        if source:
            link = db.scalar(
                select(LedgerRecordSource).where(LedgerRecordSource.source_id == source.id)
            )
            if link:
                return serialize_record(db, get_record(db, actor.owner_id, link.record_id))
    candidate, fields = parse_text(data.text, settings)
    source = CaptureSource(
        owner_id=actor.owner_id,
        source_type="text",
        source_external_id=data.external_id,
        raw_text=data.text,
        raw_payload={"fields": fields},
        parser="deterministic",
        parser_version="1",
        capture_state="parsed",
    )
    db.add(source)
    db.flush()
    record = create_record(db, actor.owner_id, candidate, idempotency_key or "", actor_id(actor))
    db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="capture"))
    db.commit()
    return serialize_record(db, get_record(db, actor.owner_id, record.id))


@router.get("/capture-sources/{source_id}")
def capture_source_get(
    source_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    row = owned(db, CaptureSource, source_id, actor.owner_id, "capture_source")
    links = db.scalars(select(LedgerRecordSource).where(LedgerRecordSource.source_id == row.id))
    return jsonable(
        {
            "id": row.id,
            "source_type": row.source_type,
            "capture_state": row.capture_state,
            "error_code": row.error_code,
            "parser": row.parser,
            "parser_version": row.parser_version,
            "record_ids": [link.record_id for link in links],
            "captured_at": row.captured_at,
        }
    )


@router.post("/capture-sources/{source_id}/retry")
def capture_source_retry(
    source_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    row = owned(db, CaptureSource, source_id, actor.owner_id, "capture_source")
    if row.capture_state == "processing":
        raise AppError(409, "capture_processing", "来源正在处理中")
    job = BackgroundJob(
        job_type="capture.parse",
        idempotency_key=f"capture:{row.id}:retry:{row.parser_version or '1'}",
        payload={"source_id": str(row.id)},
        state="pending",
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return {"status": "queued"}


@router.post("/capture/assets/init")
def asset_init(
    data: AssetInit,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.capture")),
    settings: Settings = Depends(get_settings),
):
    if not idempotency_key:
        raise AppError(400, "idempotency_key_required", "必须提供 Idempotency-Key")
    return AssetClient(settings).init_upload(actor.owner_id, data, idempotency_key)


@router.post("/capture/assets/complete", status_code=201)
def asset_complete(
    data: AssetComplete,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.capture")),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise AppError(400, "idempotency_key_required", "必须提供 Idempotency-Key")
    existing = db.scalar(
        select(CaptureSource).where(
            CaptureSource.owner_id == actor.owner_id,
            CaptureSource.source_type == "asset",
            CaptureSource.source_external_id == data.upload_id,
        )
    )
    if existing:
        return {"capture_source_id": str(existing.id), "status": existing.capture_state}
    asset_id = AssetClient(settings).complete_upload(data.upload_id)
    source = CaptureSource(
        owner_id=actor.owner_id,
        source_type="asset",
        source_external_id=data.upload_id,
        asset_id=asset_id,
        capture_state="received",
    )
    db.add(source)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        source = db.scalar(
            select(CaptureSource).where(
                CaptureSource.owner_id == actor.owner_id,
                CaptureSource.source_type == "asset",
                CaptureSource.source_external_id == data.upload_id,
            )
        )
        assert source is not None
        return {"capture_source_id": str(source.id), "status": source.capture_state}
    binding = AssetBinding(
        owner_id=actor.owner_id,
        source_type="capture_source",
        source_id=source.id,
        asset_id=asset_id,
        usage=data.usage,
    )
    db.add(binding)
    db.add(
        OutboxEvent(
            event_type="ledger.asset.reference.create",
            aggregate_type="capture_source",
            aggregate_id=source.id,
            payload={
                "asset_id": str(asset_id),
                "reference_key": f"ledger:capture_source:{source.id}:{data.usage}",
                "target_uri": f"shadow://ledger/capture-sources/{source.id}",
                "usage": data.usage,
            },
        )
    )
    db.add(
        BackgroundJob(
            job_type="capture.parse",
            idempotency_key=f"capture:{source.id}:initial",
            payload={"source_id": str(source.id)},
            state="pending",
        )
    )
    db.commit()
    return {"capture_source_id": str(source.id), "status": "received"}


@router.post("/imports/preview")
def imports_preview(data: ImportPreview, actor: Actor = Depends(current_actor)):
    try:
        if data.format == "json":
            import json

            rows = json.loads(data.content)
            if not isinstance(rows, list):
                rows = [rows]
        else:
            rows = list(csv.DictReader(io.StringIO(data.content)))
    except (ValueError, csv.Error) as exc:
        raise AppError(422, "invalid_import", "导入内容无法解析") from exc
    if len(rows) > 1000:
        raise AppError(413, "import_too_large", "单次导入最多 1000 行")
    return {
        "row_count": len(rows),
        "columns": sorted({key for row in rows if isinstance(row, dict) for key in row}),
        "warnings": [],
    }


@router.post("/imports/commit", status_code=201)
def imports_commit(
    data: ImportCommit,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.capture")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise AppError(400, "idempotency_key_required", "必须提供 Idempotency-Key")
    source = db.scalar(
        select(CaptureSource).where(
            CaptureSource.owner_id == actor.owner_id,
            CaptureSource.source_type == "import",
            CaptureSource.source_external_id == (data.external_id or idempotency_key),
        )
    )
    if source:
        links = db.scalars(
            select(LedgerRecordSource).where(LedgerRecordSource.source_id == source.id)
        )
        return {"record_ids": [str(link.record_id) for link in links], "replayed": True}
    source = CaptureSource(
        owner_id=actor.owner_id,
        source_type="import",
        source_external_id=data.external_id or idempotency_key,
        raw_payload={"record_count": len(data.records)},
        parser="structured-import",
        parser_version="1",
        capture_state="parsed",
    )
    db.add(source)
    db.flush()
    ids = []
    for index, record_data in enumerate(data.records):
        record_data = record_data.model_copy(update={"confirm": False})
        record = create_record(
            db, actor.owner_id, record_data, f"{idempotency_key}:{index}", actor_id(actor)
        )
        db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="import"))
        ids.append(str(record.id))
    db.commit()
    return {"record_ids": ids, "replayed": False}


@router.get("/merchants")
def merchants_list(
    query: str | None = None, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    stmt = select(Merchant).where(Merchant.owner_id == actor.owner_id)
    if query:
        stmt = stmt.where(Merchant.canonical_name.ilike(f"%{query[:200]}%"))
    rows = db.scalars(stmt.order_by(Merchant.canonical_name).limit(100))
    return {
        "items": [
            simple_model(
                row,
                (
                    "id",
                    "canonical_name",
                    "merchant_type",
                    "place_ref",
                    "canonical_merchant_id",
                    "active",
                ),
            )
            for row in rows
        ]
    }


@router.post("/merchants", status_code=201)
def merchants_create(
    data: MerchantCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    if data.place_ref and not data.place_ref.startswith("shadow://travel/"):
        raise AppError(422, "invalid_place_ref", "地点引用必须是 shadow://travel/ URI")
    replay = replayed_create(
        db, actor, "merchants.create", idempotency_key, data.model_dump(), Merchant, "merchant"
    )
    if replay:
        return simple_model(
            replay,
            (
                "id",
                "canonical_name",
                "merchant_type",
                "place_ref",
                "canonical_merchant_id",
                "active",
            ),
        )
    row = Merchant(owner_id=actor.owner_id, **data.model_dump())
    db.add(row)
    db.flush()
    remember_create(db, actor, "merchants.create", idempotency_key or "", data.model_dump(), row.id)
    db.commit()
    return simple_model(
        row,
        ("id", "canonical_name", "merchant_type", "place_ref", "canonical_merchant_id", "active"),
    )


@router.get("/merchants/{merchant_id}")
def merchants_get(
    merchant_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    row = owned(db, Merchant, merchant_id, actor.owner_id, "merchant")
    aliases = db.scalars(select(MerchantAlias).where(MerchantAlias.merchant_id == row.id))
    result = simple_model(
        row,
        ("id", "canonical_name", "merchant_type", "place_ref", "canonical_merchant_id", "active"),
    )
    result["aliases"] = [
        simple_model(item, ("id", "alias", "normalized_alias", "source")) for item in aliases
    ]
    return result


@router.patch("/merchants/{merchant_id}")
def merchants_patch(
    merchant_id: uuid.UUID,
    data: MerchantCreate,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, Merchant, merchant_id, actor.owner_id, "merchant")
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return merchants_get(merchant_id, actor, db)


def normalize(value: str) -> str:
    return "".join(value.casefold().split())


@router.post("/merchants/{merchant_id}/aliases", status_code=201)
def merchant_alias(
    merchant_id: uuid.UUID,
    data: AliasCreate,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    owned(db, Merchant, merchant_id, actor.owner_id, "merchant")
    row = MerchantAlias(
        merchant_id=merchant_id,
        alias=data.alias,
        normalized_alias=normalize(data.alias),
        source=data.source,
    )
    db.add(row)
    db.commit()
    return simple_model(row, ("id", "alias", "normalized_alias", "source"))


@router.post("/merchants/{merchant_id}/merge")
def merchant_merge(
    merchant_id: uuid.UUID,
    data: MergeCreate,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    source = owned(db, Merchant, merchant_id, actor.owner_id, "merchant")
    target = owned(db, Merchant, data.target_id, actor.owner_id, "merchant")
    if source.id == target.id:
        raise AppError(422, "self_merge", "不能合并到自身")
    source.canonical_merchant_id = target.id
    source.active = False
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type="user",
            actor_id=actor_id(actor),
            action="merchant.merged",
            aggregate_type="merchant",
            aggregate_id=source.id,
            details={"target_id": str(target.id), "reason": data.reason},
        )
    )
    db.commit()
    return merchants_get(merchant_id, actor, db)


@router.get("/items")
def items_list(
    query: str | None = None, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    stmt = select(ItemIdentity).where(ItemIdentity.owner_id == actor.owner_id)
    if query:
        stmt = stmt.where(ItemIdentity.canonical_name.ilike(f"%{query[:200]}%"))
    rows = db.scalars(stmt.order_by(ItemIdentity.canonical_name).limit(100))
    fields = (
        "id",
        "kind",
        "canonical_name",
        "brand",
        "variant",
        "merchant_id",
        "barcode",
        "canonical_item_id",
        "active",
    )
    return {"items": [simple_model(row, fields) for row in rows]}


@router.post("/items", status_code=201)
def items_create(
    data: ItemCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    if data.merchant_id:
        owned(db, Merchant, data.merchant_id, actor.owner_id, "merchant")
    replay = replayed_create(
        db, actor, "items.create", idempotency_key, data.model_dump(), ItemIdentity, "item"
    )
    if replay:
        return simple_model(
            replay,
            (
                "id",
                "kind",
                "canonical_name",
                "brand",
                "variant",
                "merchant_id",
                "barcode",
                "external_ids",
                "canonical_item_id",
                "active",
            ),
        )
    row = ItemIdentity(owner_id=actor.owner_id, **data.model_dump())
    db.add(row)
    db.flush()
    remember_create(db, actor, "items.create", idempotency_key or "", data.model_dump(), row.id)
    db.commit()
    return simple_model(
        row,
        (
            "id",
            "kind",
            "canonical_name",
            "brand",
            "variant",
            "merchant_id",
            "barcode",
            "external_ids",
            "canonical_item_id",
            "active",
        ),
    )


@router.get("/items/{item_id}")
def items_get(
    item_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    row = owned(db, ItemIdentity, item_id, actor.owner_id, "item")
    aliases = db.scalars(select(ItemAlias).where(ItemAlias.item_identity_id == row.id))
    result = simple_model(
        row,
        (
            "id",
            "kind",
            "canonical_name",
            "brand",
            "variant",
            "merchant_id",
            "barcode",
            "external_ids",
            "canonical_item_id",
            "active",
        ),
    )
    result["aliases"] = [
        simple_model(item, ("id", "alias", "normalized_alias", "source")) for item in aliases
    ]
    return result


@router.patch("/items/{item_id}")
def items_patch(
    item_id: uuid.UUID,
    data: ItemCreate,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, ItemIdentity, item_id, actor.owner_id, "item")
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return items_get(item_id, actor, db)


@router.post("/items/{item_id}/aliases", status_code=201)
def item_alias(
    item_id: uuid.UUID,
    data: AliasCreate,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    owned(db, ItemIdentity, item_id, actor.owner_id, "item")
    row = ItemAlias(
        item_identity_id=item_id,
        alias=data.alias,
        normalized_alias=normalize(data.alias),
        source=data.source,
    )
    db.add(row)
    db.commit()
    return simple_model(row, ("id", "alias", "normalized_alias", "source"))


@router.post("/items/{item_id}/merge")
def item_merge(
    item_id: uuid.UUID,
    data: MergeCreate,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    source = owned(db, ItemIdentity, item_id, actor.owner_id, "item")
    target = owned(db, ItemIdentity, data.target_id, actor.owner_id, "item")
    if source.id == target.id:
        raise AppError(422, "self_merge", "不能合并到自身")
    source.canonical_item_id = target.id
    source.active = False
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type="user",
            actor_id=actor_id(actor),
            action="item.merged",
            aggregate_type="item",
            aggregate_id=source.id,
            details={"target_id": str(target.id), "reason": data.reason},
        )
    )
    db.commit()
    return items_get(item_id, actor, db)


@router.get("/identity-suggestions")
def suggestions(actor: Actor = Depends(current_actor), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(IdentitySuggestion).where(
            IdentitySuggestion.owner_id == actor.owner_id, IdentitySuggestion.state == "pending"
        )
    )
    return {
        "items": [
            simple_model(row, ("id", "source_type", "source_id", "target_id", "reason", "state"))
            for row in rows
        ]
    }


@router.post("/identity-suggestions/{suggestion_id}/{decision}")
def suggestion_decide(
    suggestion_id: uuid.UUID,
    decision: str,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    if decision not in {"accept", "reject"}:
        raise AppError(404, "route_not_found", "操作不存在")
    row = owned(db, IdentitySuggestion, suggestion_id, actor.owner_id, "suggestion")
    row.state = "accepted" if decision == "accept" else "rejected"
    db.commit()
    return simple_model(row, ("id", "state"))


INTENT_FIELDS = (
    "id",
    "intent_type",
    "title",
    "item_identity_id",
    "merchant_id",
    "place_ref",
    "expected_amount",
    "currency",
    "desired_start",
    "desired_end",
    "priority",
    "state",
    "reason",
    "completed_record_id",
    "revision",
)


@router.get("/intents")
def intents_list(
    state: str | None = None, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    stmt = select(SpendingIntent).where(SpendingIntent.owner_id == actor.owner_id)
    if state:
        stmt = stmt.where(SpendingIntent.state == state)
    return {
        "items": [
            simple_model(row, INTENT_FIELDS)
            for row in db.scalars(stmt.order_by(SpendingIntent.created_at.desc()))
        ]
    }


@router.post("/intents", status_code=201)
def intents_create(
    data: IntentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    replay = replayed_create(
        db, actor, "intents.create", idempotency_key, data.model_dump(), SpendingIntent, "intent"
    )
    if replay:
        return simple_model(replay, INTENT_FIELDS)
    row = SpendingIntent(owner_id=actor.owner_id, **data.model_dump())
    db.add(row)
    db.flush()
    remember_create(db, actor, "intents.create", idempotency_key or "", data.model_dump(), row.id)
    db.commit()
    return simple_model(row, INTENT_FIELDS)


@router.get("/intents/{intent_id}")
def intents_get(
    intent_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    return simple_model(
        owned(db, SpendingIntent, intent_id, actor.owner_id, "intent"), INTENT_FIELDS
    )


@router.patch("/intents/{intent_id}")
def intents_patch(
    intent_id: uuid.UUID,
    data: IntentCreate,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, SpendingIntent, intent_id, actor.owner_id, "intent")
    expected = parse_etag(if_match)
    if row.revision != expected:
        raise AppError(409, "revision_conflict", "计划已被更新")
    if data.state == "completed":
        raise AppError(422, "record_required", "完成计划必须关联已确认记录")
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    row.revision += 1
    db.commit()
    etag(response, row.revision)
    return simple_model(row, INTENT_FIELDS)


@router.post("/intents/{intent_id}/draft", status_code=201)
def intent_draft(
    intent_id: uuid.UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    row = owned(db, SpendingIntent, intent_id, actor.owner_id, "intent")
    money = None
    if row.expected_amount:
        money = MoneyEntryInput(
            type="expense", amount=row.expected_amount, currency=row.currency, title=row.title
        )
    scene = "dine_in" if row.intent_type in {"eat", "drink"} else "offline_purchase"
    data = RecordCreate(
        occurred_at=datetime.now(UTC),
        timezone=settings.default_timezone,
        money_entry=money,
        consumption={
            "scene": scene,
            "merchant_id": row.merchant_id,
            "lines": [{"raw_name": row.title, "item_identity_id": row.item_identity_id}],
        },
    )
    record = create_record(db, actor.owner_id, data, idempotency_key or "", actor_id(actor))
    etag(response, record.revision)
    return serialize_record(db, record)


@router.post("/intents/{intent_id}/complete")
def intent_complete(
    intent_id: uuid.UUID,
    record_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, SpendingIntent, intent_id, actor.owner_id, "intent")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "计划已被更新")
    record = get_record(db, actor.owner_id, record_id)
    if record.state != "confirmed":
        raise AppError(422, "confirmed_record_required", "只能关联已确认记录")
    row.state = "completed"
    row.completed_record_id = record.id
    row.revision += 1
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type="user",
            actor_id=actor_id(actor),
            action="intent.completed",
            aggregate_type="intent",
            aggregate_id=row.id,
            details={"record_id": str(record.id)},
        )
    )
    db.commit()
    return simple_model(row, INTENT_FIELDS)


COMMITMENT_FIELDS = (
    "id",
    "kind",
    "title",
    "merchant_id",
    "item_identity_id",
    "expected_amount",
    "currency",
    "recurrence_rule",
    "timezone",
    "next_due_at",
    "auto_renew",
    "remind_before_seconds",
    "state",
    "last_record_id",
    "revision",
)


@router.get("/recurring-commitments")
def commitments_list(actor: Actor = Depends(current_actor), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(RecurringCommitment)
        .where(RecurringCommitment.owner_id == actor.owner_id)
        .order_by(RecurringCommitment.next_due_at)
    )
    return {"items": [simple_model(row, COMMITMENT_FIELDS) for row in rows]}


@router.post("/recurring-commitments", status_code=201)
def commitments_create(
    data: CommitmentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    replay = replayed_create(
        db,
        actor,
        "commitments.create",
        idempotency_key,
        data.model_dump(),
        RecurringCommitment,
        "commitment",
    )
    if replay:
        return simple_model(replay, COMMITMENT_FIELDS)
    row = RecurringCommitment(owner_id=actor.owner_id, state="active", **data.model_dump())
    db.add(row)
    db.flush()
    remember_create(
        db, actor, "commitments.create", idempotency_key or "", data.model_dump(), row.id
    )
    db.commit()
    return simple_model(row, COMMITMENT_FIELDS)


@router.get("/recurring-commitments/{commitment_id}")
def commitments_get(
    commitment_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    return simple_model(
        owned(db, RecurringCommitment, commitment_id, actor.owner_id, "commitment"),
        COMMITMENT_FIELDS,
    )


@router.patch("/recurring-commitments/{commitment_id}")
def commitments_patch(
    commitment_id: uuid.UUID,
    data: CommitmentCreate,
    response: Response,
    state: str = "active",
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, RecurringCommitment, commitment_id, actor.owner_id, "commitment")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "周期事项已被更新")
    if state not in {"active", "paused", "ended"}:
        raise AppError(422, "invalid_state", "周期事项状态无效")
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    row.state = state
    row.revision += 1
    db.commit()
    etag(response, row.revision)
    return simple_model(row, COMMITMENT_FIELDS)


def recurring_money_type(kind: str) -> str:
    return "income" if kind == "regular_income" else "expense"


@router.post("/recurring-commitments/{commitment_id}/draft", status_code=201)
def commitment_draft(
    commitment_id: uuid.UUID,
    occurrence: datetime | None = None,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, RecurringCommitment, commitment_id, actor.owner_id, "commitment")
    occurrence = occurrence or row.next_due_at
    key = f"commitment:{row.id}:occurrence:{occurrence.isoformat()}"
    existing = db.scalar(
        select(Reminder).where(Reminder.owner_id == actor.owner_id, Reminder.reminder_key == key)
    )
    if existing and existing.payload.get("record_id"):
        return serialize_record(
            db, get_record(db, actor.owner_id, uuid.UUID(existing.payload["record_id"]))
        )
    reminder = existing or Reminder(
        owner_id=actor.owner_id,
        reminder_key=key,
        source_type="commitment",
        source_id=row.id,
        due_at=occurrence,
        state="pending",
        payload={},
    )
    db.add(reminder)
    if row.expected_amount is None:
        raise AppError(422, "amount_required", "请先填写预计金额，或手工创建无金额消费")
    data = RecordCreate(
        occurred_at=occurrence,
        timezone=row.timezone,
        money_entry=MoneyEntryInput(
            type=recurring_money_type(row.kind),
            amount=row.expected_amount,
            currency=row.currency,
            title=row.title,
        ),
    )
    record = create_record(db, actor.owner_id, data, key, actor_id(actor))
    reminder.payload = {"record_id": str(record.id)}
    reminder.state = "handled"
    row.last_record_id = record.id
    db.commit()
    return serialize_record(db, record)


@router.get("/reminders")
def reminders_list(actor: Actor = Depends(current_actor), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Reminder).where(Reminder.owner_id == actor.owner_id).order_by(Reminder.due_at)
    )
    return {
        "items": [
            simple_model(
                row,
                ("id", "reminder_key", "source_type", "source_id", "due_at", "state", "payload"),
            )
            for row in rows
        ]
    }


@router.post("/reminders/{reminder_id}/{action}")
def reminder_action(
    reminder_id: uuid.UUID,
    action: str,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    states = {"read": "read", "dismiss": "dismissed"}
    if action not in states:
        raise AppError(404, "route_not_found", "操作不存在")
    row = owned(db, Reminder, reminder_id, actor.owner_id, "reminder")
    row.state = states[action]
    db.commit()
    return simple_model(row, ("id", "state"))


BUDGET_FIELDS = (
    "id",
    "category_id",
    "budget_month",
    "monthly_amount",
    "currency",
    "active",
    "revision",
)


@router.get("/budget-targets")
def budgets_list(
    month: date | None = None, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    stmt = select(BudgetTarget).where(BudgetTarget.owner_id == actor.owner_id)
    if month:
        stmt = stmt.where(BudgetTarget.budget_month == month.replace(day=1))
    return {"items": [simple_model(row, BUDGET_FIELDS) for row in db.scalars(stmt)]}


@router.post("/budget-targets", status_code=201)
def budgets_create(
    data: BudgetCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    if data.category_id:
        owned(db, MoneyCategory, data.category_id, actor.owner_id, "category")
    replay = replayed_create(
        db, actor, "budgets.create", idempotency_key, data.model_dump(), BudgetTarget, "budget"
    )
    if replay:
        return simple_model(replay, BUDGET_FIELDS)
    row = BudgetTarget(owner_id=actor.owner_id, active=True, **data.model_dump())
    db.add(row)
    try:
        db.flush()
        remember_create(
            db, actor, "budgets.create", idempotency_key or "", data.model_dump(), row.id
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(409, "budget_exists", "该月份目标已存在") from exc
    return simple_model(row, BUDGET_FIELDS)


@router.get("/budget-targets/{budget_id}")
def budgets_get(
    budget_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    return simple_model(owned(db, BudgetTarget, budget_id, actor.owner_id, "budget"), BUDGET_FIELDS)


@router.patch("/budget-targets/{budget_id}")
def budgets_patch(
    budget_id: uuid.UUID,
    data: BudgetCreate,
    response: Response,
    active: bool = True,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, BudgetTarget, budget_id, actor.owner_id, "budget")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "目标已被更新")
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    row.active = active
    row.revision += 1
    db.commit()
    etag(response, row.revision)
    return simple_model(row, BUDGET_FIELDS)


def month_range(month: str | None) -> tuple[datetime, datetime]:
    try:
        start_date = (
            datetime.strptime(month, "%Y-%m")
            if month
            else datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=UTC)
    except ValueError as exc:
        raise AppError(400, "invalid_month", "月份格式应为 YYYY-MM") from exc
    return start_date, start_date + relativedelta(months=1)


@router.get("/insights/summary")
def insight_summary(
    month: str | None = None,
    currency: str = "CNY",
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    start, end = month_range(month)
    sums = db.execute(
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
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            MoneyEntry.currency == currency.upper(),
        )
    ).one()
    expense, income, refund = map(Decimal, sums)
    return jsonable(
        {
            "range": {"from": start, "to": end},
            "currency": currency.upper(),
            "generated_at": datetime.now(UTC),
            "expense": expense,
            "income": income,
            "refund": refund,
            "net_spending": expense - refund,
            "net_income": income - expense + refund,
        }
    )


@router.get("/insights/categories")
def insight_categories(
    month: str | None = None,
    currency: str = "CNY",
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    start, end = month_range(month)
    rows = db.execute(
        select(
            MoneyCategory.key,
            MoneyCategory.name,
            func.sum(
                case((MoneyEntry.type == "refund", -MoneyEntry.amount), else_=MoneyEntry.amount)
            ),
        )
        .select_from(MoneyEntry)
        .join(LedgerRecord)
        .outerjoin(MoneyCategory)
        .where(
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            MoneyEntry.currency == currency.upper(),
            MoneyEntry.type.in_(["expense", "refund"]),
        )
        .group_by(MoneyCategory.key, MoneyCategory.name)
    ).all()
    return jsonable(
        {
            "range": {"from": start, "to": end},
            "currency": currency.upper(),
            "items": [
                {"category_key": key, "name": name or "未分类", "net_spending": amount}
                for key, name, amount in rows
            ],
        }
    )


@router.get("/insights/scenes")
def insight_scenes(
    month: str | None = None, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    start, end = month_range(month)
    rows = db.execute(
        select(ConsumptionEvent.scene, func.count())
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
        )
        .group_by(ConsumptionEvent.scene)
    ).all()
    return jsonable(
        {
            "range": {"from": start, "to": end},
            "items": [{"scene": scene, "count": count} for scene, count in rows],
        }
    )


@router.get("/insights/merchants")
def insight_merchants(actor: Actor = Depends(current_actor), db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            ConsumptionEvent.merchant_id,
            ConsumptionEvent.merchant_name_raw,
            func.count(),
            func.sum(MoneyEntry.amount),
        )
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .outerjoin(MoneyEntry, ConsumptionEvent.money_entry_id == MoneyEntry.id)
        .where(LedgerRecord.owner_id == actor.owner_id, LedgerRecord.state == "confirmed")
        .group_by(ConsumptionEvent.merchant_id, ConsumptionEvent.merchant_name_raw)
        .order_by(func.count().desc())
        .limit(50)
    ).all()
    return jsonable(
        {
            "items": [
                {
                    "merchant_id": merchant_id,
                    "merchant_name_raw": name,
                    "count": count,
                    "amount": amount,
                }
                for merchant_id, name, count, amount in rows
            ]
        }
    )


@router.get("/insights/items")
def insight_items(actor: Actor = Depends(current_actor), db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            ConsumptionLine.item_identity_id,
            ConsumptionLine.raw_name,
            func.count(),
            func.max(LedgerRecord.occurred_at),
            func.min(LedgerRecord.occurred_at),
        )
        .select_from(ConsumptionLine)
        .join(ConsumptionEvent, ConsumptionLine.event_id == ConsumptionEvent.id)
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(LedgerRecord.owner_id == actor.owner_id, LedgerRecord.state == "confirmed")
        .group_by(ConsumptionLine.item_identity_id, ConsumptionLine.raw_name)
        .order_by(func.count().desc())
        .limit(50)
    ).all()
    return jsonable(
        {
            "items": [
                {
                    "item_identity_id": item_id,
                    "raw_name": name,
                    "count": count,
                    "first_at": first,
                    "last_at": last,
                }
                for item_id, name, count, last, first in rows
            ]
        }
    )


@router.get("/insights/budgets")
def insight_budgets(
    month: str, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    start, end = month_range(month)
    targets = db.scalars(
        select(BudgetTarget).where(
            BudgetTarget.owner_id == actor.owner_id,
            BudgetTarget.budget_month == start.date(),
            BudgetTarget.active.is_(True),
        )
    )
    items = []
    for target in targets:
        stmt = (
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
                LedgerRecord.owner_id == actor.owner_id,
                LedgerRecord.state == "confirmed",
                LedgerRecord.occurred_at >= start,
                LedgerRecord.occurred_at < end,
                MoneyEntry.currency == target.currency,
                MoneyEntry.type.in_(["expense", "refund"]),
            )
        )
        if target.category_id:
            stmt = stmt.where(MoneyEntry.category_id == target.category_id)
        spent = Decimal(db.scalar(stmt) or 0)
        items.append(
            {
                "target": simple_model(target, BUDGET_FIELDS),
                "net_spending": format(spent, "f"),
                "remaining": format(target.monthly_amount - spent, "f"),
            }
        )
    unknown = db.scalar(
        select(func.count())
        .select_from(LedgerRecord)
        .join(ConsumptionEvent)
        .where(
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            ConsumptionEvent.money_entry_id.is_(None),
        )
    )
    return {"items": items, "unknown_amount_records": unknown}


@router.post("/records/{record_id}/references", status_code=201)
def references_create(
    record_id: uuid.UUID,
    data: ReferenceCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.integrations")),
    db: Session = Depends(get_db),
):
    get_record(db, actor.owner_id, record_id)
    payload = {"record_id": record_id, **data.model_dump()}
    replay = replayed_create(
        db, actor, "references.create", idempotency_key, payload, ExternalReference, "reference"
    )
    if replay:
        return simple_model(replay, ("id", "relation", "target_uri", "created_at"))
    row = ExternalReference(
        owner_id=actor.owner_id,
        source_type="record",
        source_id=record_id,
        relation=data.relation,
        target_uri=data.target_uri,
    )
    db.add(row)
    db.flush()
    remember_create(db, actor, "references.create", idempotency_key or "", payload, row.id)
    db.commit()
    return simple_model(row, ("id", "relation", "target_uri", "created_at"))


@router.get("/records/{record_id}/references")
def references_list(
    record_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    get_record(db, actor.owner_id, record_id)
    rows = db.scalars(
        select(ExternalReference).where(
            ExternalReference.owner_id == actor.owner_id,
            ExternalReference.source_type == "record",
            ExternalReference.source_id == record_id,
        )
    )
    return {
        "items": [simple_model(row, ("id", "relation", "target_uri", "created_at")) for row in rows]
    }


@router.delete("/records/{record_id}/references/{reference_id}", status_code=204)
def references_delete(
    record_id: uuid.UUID,
    reference_id: uuid.UUID,
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, ExternalReference, reference_id, actor.owner_id, "reference")
    if row.source_type != "record" or row.source_id != record_id:
        raise AppError(404, "reference_not_found", "引用不存在")
    db.delete(row)
    db.commit()


@router.post("/exports", status_code=202)
def export_create(
    format: str = "json",
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(current_actor),
    db: Session = Depends(get_db),
):
    if format not in {"json", "csv"}:
        raise AppError(422, "invalid_export_format", "导出格式仅支持 json 或 csv")
    payload = {"format": format}
    replay = idempotency_lookup(db, actor.owner_id, "exports.create", idempotency_key, payload)
    if replay and replay.resource_id:
        job = db.get(BackgroundJob, replay.resource_id)
        if job:
            return {"id": str(job.id), "state": job.state}
    job = BackgroundJob(
        job_type="export",
        idempotency_key=f"export:{actor.owner_id}:{format}:{uuid.uuid4()}",
        payload={"owner_id": actor.owner_id, "format": format},
        state="pending",
    )
    db.add(job)
    db.flush()
    remember_create(db, actor, "exports.create", idempotency_key or "", payload, job.id)
    db.commit()
    return {"id": str(job.id), "state": job.state}


@router.get("/exports/{export_id}")
def export_get(
    export_id: uuid.UUID, actor: Actor = Depends(current_actor), db: Session = Depends(get_db)
):
    job = db.get(BackgroundJob, export_id)
    if job is None or job.job_type != "export" or job.payload.get("owner_id") != actor.owner_id:
        raise AppError(404, "export_not_found", "导出任务不存在")
    return jsonable(
        {
            "id": job.id,
            "state": job.state,
            "error_code": job.last_error_code,
            "asset_id": job.payload.get("asset_id"),
        }
    )
