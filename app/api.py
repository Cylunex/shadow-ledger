from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.importers import PARSER_VERSION, ParsedImport
from app.integrations import AssetClient
from app.models import (
    ArchiveEvidenceLink,
    AssetBinding,
    AuditEvent,
    BackgroundJob,
    BudgetTarget,
    CaptureSource,
    ConsumptionEvent,
    ConsumptionLine,
    ExternalReference,
    ForecastItem,
    ForecastRun,
    IdentitySuggestion,
    ImportBatch,
    ImportReviewItem,
    ItemAlias,
    ItemIdentity,
    LedgerRecord,
    LedgerRecordSource,
    Merchant,
    MerchantAlias,
    MerchantNormalizationRule,
    MoneyCategory,
    MoneyEntry,
    OutboxEvent,
    RecurringCommitment,
    Reminder,
    SpendingIntent,
    UseCycle,
)
from app.payments import PAYMENT_METHOD_LABELS, PaymentMethod
from app.schemas import (
    AliasCreate,
    ArchiveEvidenceCreate,
    ArchiveEvidenceRelease,
    AssetComplete,
    AssetInit,
    BatchConfirm,
    BudgetCreate,
    CategoryCreate,
    CategoryPatch,
    CommitmentCreate,
    ForecastGenerate,
    ImportCommit,
    ImportPreview,
    ImportReviewResolve,
    IntentCreate,
    ItemCreate,
    MerchantCreate,
    MergeCreate,
    MoneyEntryInput,
    RecordCreate,
    RecordPatch,
    ReferenceCreate,
    RuleRevoke,
    StructuredIntake,
    TextCaptureCreate,
    UseCycleCreate,
    UseCyclePatch,
    UseCycleTransition,
    jsonable,
)
from app.security import Actor, require_scope, user_actor
from app.services.feedback import decorate_run, save_feedback
from app.services.forecast import generate_forecast, serialize_run, verify_run
from app.services.import_review import (
    amount_anomaly_reason,
    apply_normalization_rule,
    quality_metrics,
    refund_candidate,
    serialize_review_item,
)
from app.services.intake import ingest_structured
from app.services.payment_imports import parse_text_import
from app.services.records import (
    add_money_entry,
    confirm_record,
    confirm_records,
    create_record,
    delete_draft,
    get_record,
    idempotency_lookup,
    idempotency_save,
    list_records,
    lock_command,
    parse_etag,
    patch_record,
    request_hash,
    serialize_record,
    void_record,
)

router = APIRouter(prefix="/api/v1")


def etag(response: Response, revision: int) -> None:
    response.headers["ETag"] = f'"{revision}"'


def actor_id(actor: Actor) -> str:
    return actor.owner_id if actor.actor_type == "user" else "service"


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def owned(db: Session, model, resource_id: uuid.UUID, owner_id: str, name: str):
    row = db.scalar(select(model).where(model.id == resource_id, model.owner_id == owner_id))
    if row is None:
        raise AppError(404, f"{name}_not_found", "资源不存在")
    return row


def simple_model(row, fields: tuple[str, ...]) -> dict[str, Any]:
    return jsonable({field: getattr(row, field) for field in fields})


def owned_locked(db, model, resource_id, owner_id, name):
    row = db.scalar(
        select(model)
        .where(model.id == resource_id, model.owner_id == owner_id)
        .with_for_update(of=model)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise AppError(404, f"{name}_not_found", "资源不存在")
    return row


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
def me(actor: Actor = Depends(user_actor), settings: Settings = Depends(get_settings)):
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


@router.get("/payment-methods")
def payment_methods(actor: Actor = Depends(require_scope("ledger.read"))):
    return {"items": [{"key": key, "label": label} for key, label in PAYMENT_METHOD_LABELS.items()]}


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
    payment_method: PaymentMethod | None = None,
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
        payment_method=payment_method,
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
        db,
        actor.owner_id,
        record_id,
        parse_etag(if_match),
        data,
        actor_id(actor),
        draft_only="ledger.confirm" not in actor.scopes,
        actor_type=actor.actor_type,
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


@router.post("/records/batch-confirm")
def records_batch_confirm(
    data: BatchConfirm,
    actor: Actor = Depends(require_scope("ledger.confirm")),
    db: Session = Depends(get_db),
):
    records = confirm_records(
        db,
        actor.owner_id,
        [(item.id, item.revision) for item in data.records],
        actor_id(actor),
    )
    return {
        "confirmed_count": len(records),
        "record_ids": [str(record.id) for record in records],
    }


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
        db,
        actor.owner_id,
        record_id,
        parse_etag(if_match),
        data,
        actor_id(actor),
        draft_only="ledger.confirm" not in actor.scopes,
        actor_type=actor.actor_type,
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
    lock_command(db, owner_id, "categories.seed", "defaults")
    existing = set(db.scalars(select(MoneyCategory.key).where(MoneyCategory.owner_id == owner_id)))
    added = False
    for order, (key, name) in enumerate(DEFAULT_CATEGORIES):
        if key not in existing:
            db.add(MoneyCategory(owner_id=owner_id, key=key, name=name, sort_order=order))
            added = True
    if added:
        db.commit()


@router.get("/categories")
def categories_list(actor: Actor = Depends(user_actor), db: Session = Depends(get_db)):
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
    actor: Actor = Depends(user_actor),
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
    actor: Actor = Depends(user_actor),
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
        lock_command(db, actor.owner_id, "source.text", data.external_id)
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
    record = create_record(
        db,
        actor.owner_id,
        candidate,
        f"capture:{request_hash(idempotency_key).hex()}",
        actor_id(actor),
        actor_type=actor.actor_type,
        commit=False,
    )
    db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="capture"))
    from app.services.intake_reviews import register_intake_reviews
    from app.services.sources import ensure_baseline

    ensure_baseline(db, source)
    register_intake_reviews(db, actor.owner_id, source, [record], [candidate])
    idempotency_save(
        db, actor.owner_id, "capture.text", idempotency_key, data.model_dump(), record.id
    )
    db.commit()
    return serialize_record(db, get_record(db, actor.owner_id, record.id))


@router.get("/capture-sources/{source_id}")
def capture_source_get(
    source_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    source_id: uuid.UUID,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.capture")),
    db: Session = Depends(get_db),
):
    payload = {"source_id": str(source_id)}
    replay = idempotency_lookup(db, actor.owner_id, "capture.retry", idempotency_key, payload)
    if replay:
        return {"status": "queued", "replayed": True}
    row = owned(db, CaptureSource, source_id, actor.owner_id, "capture_source")
    row = db.scalar(select(CaptureSource).where(CaptureSource.id == row.id).with_for_update())
    if row.capture_state != "failed":
        raise AppError(409, "capture_not_failed", "仅可重试失败的采集来源")
    row.capture_state = "received"
    row.error_code = None
    job = BackgroundJob(
        job_type="capture.parse",
        idempotency_key=f"capture:{row.id}:retry:{idempotency_key}",
        payload={"source_id": str(row.id)},
        state="pending",
    )
    db.add(job)
    idempotency_save(db, actor.owner_id, "capture.retry", idempotency_key, payload, row.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(409, "capture_retry_conflict", "重试请求发生冲突，请刷新状态") from exc
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
def imports_preview(data: ImportPreview, actor: Actor = Depends(user_actor)):
    if data.format in {"markdown", "csv"}:
        try:
            parsed = parse_text_import(data.content, data.format)
        except ValueError as exc:
            raise AppError(422, "invalid_import", str(exc)) from exc
        return {
            "platform": parsed.platform,
            "row_count": parsed.row_count,
            "record_count": len(parsed.candidates),
            "skipped_count": parsed.skipped_count,
            "columns": list(parsed.columns),
            "warnings": list(parsed.warnings),
            "items": [
                {
                    "source_external_id": item.source_external_id,
                    "record": jsonable(item.record.model_dump()),
                    "warnings": list(item.warnings),
                }
                for item in parsed.candidates
            ],
        }
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


def _existing_import_record_ids(db: Session, source_id: uuid.UUID) -> list[str]:
    links = db.scalars(select(LedgerRecordSource).where(LedgerRecordSource.source_id == source_id))
    return [str(link.record_id) for link in links]


def _serialize_import_batch(db: Session, batch: ImportBatch, *, replayed: bool) -> dict[str, Any]:
    items = list(
        db.scalars(
            select(ImportReviewItem)
            .where(ImportReviewItem.batch_id == batch.id)
            .order_by(ImportReviewItem.created_at, ImportReviewItem.id)
        )
    )
    return {
        "batch_id": str(batch.id),
        "state": batch.state,
        "platform": batch.platform,
        "record_ids": [str(item.record_id) for item in items],
        "created_count": batch.created_count,
        "duplicate_count": batch.duplicate_count,
        "skipped_count": batch.skipped_count,
        "pending_review_count": sum(item.review_state == "pending" for item in items),
        "review_items": [serialize_review_item(item) for item in items],
        "replayed": replayed,
    }


def _commit_markdown_import(
    db: Session, actor: Actor, parsed: ParsedImport, idempotency_key: str, payload: Any
) -> ImportBatch:
    digest = request_hash(payload)
    lock_command(db, actor.owner_id, "import.batch", idempotency_key)
    existing_batch = db.scalar(
        select(ImportBatch).where(
            ImportBatch.owner_id == actor.owner_id,
            ImportBatch.idempotency_key == idempotency_key,
        )
    )
    if existing_batch:
        if existing_batch.request_hash != digest:
            raise AppError(
                409,
                "idempotency_payload_mismatch",
                "同一 Idempotency-Key 不能用于不同导入内容",
            )
        return existing_batch
    batch = ImportBatch(
        owner_id=actor.owner_id,
        idempotency_key=idempotency_key,
        request_hash=digest,
        platform=parsed.platform,
        state="open",
        row_count=parsed.row_count,
        skipped_count=parsed.skipped_count,
    )
    db.add(batch)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raced = db.scalar(
            select(ImportBatch).where(
                ImportBatch.owner_id == actor.owner_id,
                ImportBatch.idempotency_key == idempotency_key,
            )
        )
        if raced is None:
            raise
        if raced.request_hash != digest:
            raise AppError(
                409,
                "idempotency_payload_mismatch",
                "同一 Idempotency-Key 不能用于不同导入内容",
            ) from exc
        return raced
    duplicate_count = 0
    for source_key in sorted({candidate.source_external_id for candidate in parsed.candidates}):
        lock_command(db, actor.owner_id, "source.import", source_key)
    for candidate in parsed.candidates:
        existing = db.scalar(
            select(CaptureSource).where(
                CaptureSource.owner_id == actor.owner_id,
                CaptureSource.source_type == "import",
                CaptureSource.source_external_id == candidate.source_external_id,
            )
        )
        if existing:
            from app.services.sources import append_observation, comparable_payload, ensure_baseline

            ensure_baseline(db, existing)
            if comparable_payload(existing.raw_payload) != comparable_payload(
                candidate.raw_payload
            ):
                append_observation(
                    db,
                    existing,
                    f"{PARSER_VERSION}:{request_hash(candidate.raw_payload).hex()}",
                    candidate.raw_payload,
                    candidate.record,
                    parser=f"{parsed.platform}-markdown",
                    parser_version=PARSER_VERSION,
                )
            existing_ids = _existing_import_record_ids(db, existing.id)
            duplicate_count += 1
            if not existing_ids:
                raise AppError(
                    409,
                    "orphan_import_source",
                    "此来源对应的草稿已删除，原始证据仍保留；不会通过重试重新录入",
                )
            record_id = uuid.UUID(existing_ids[0])
            record = get_record(db, actor.owner_id, record_id)
            rule = None
            anomaly = None
            refund = None
            source = existing
            duplicate_of = record.id
        else:
            source = CaptureSource(
                owner_id=actor.owner_id,
                source_type="import",
                source_external_id=candidate.source_external_id,
                raw_payload=candidate.raw_payload,
                parser=f"{parsed.platform}-markdown",
                parser_version=PARSER_VERSION,
                capture_state="parsed",
            )
            db.add(source)
            db.flush()
            from app.services.sources import ensure_baseline

            ensure_baseline(db, source)
            normalized_record, rule = apply_normalization_rule(db, actor.owner_id, candidate)
            anomaly = amount_anomaly_reason(db, actor.owner_id, normalized_record)
            refund = refund_candidate(db, actor.owner_id, normalized_record, candidate.raw_payload)
            record = create_record(
                db,
                actor.owner_id,
                normalized_record,
                f"import:{candidate.source_external_id}",
                actor_id(actor),
                commit=False,
            )
            db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="import"))
            duplicate_of = None
        consumption = candidate.record.consumption
        resolution: dict[str, Any] = {
            "money_type": candidate.record.money_entry.type
            if candidate.record.money_entry
            else None,
            "normalization_rule_id": str(rule.id) if rule else None,
        }
        item = ImportReviewItem(
            owner_id=actor.owner_id,
            batch_id=batch.id,
            source_id=source.id,
            record_id=record.id,
            source_external_id=candidate.source_external_id,
            raw_merchant_name=consumption.merchant_name_raw if consumption else None,
            raw_item_names=[line.raw_name for line in consumption.lines] if consumption else [],
            normalized_merchant_id=rule.merchant_id if rule else None,
            duplicate_of_record_id=duplicate_of,
            refund_candidate_entry_id=refund.id if refund else None,
            amount_anomaly_reason=anomaly,
            resolution=resolution,
        )
        db.add(item)
        db.flush()
        has_pending = bool(
            duplicate_of
            or anomaly
            or (candidate.record.money_entry and candidate.record.money_entry.type == "refund")
            or (consumption and consumption.merchant_name_raw and rule is None)
        )
        if not has_pending:
            item.review_state = "resolved"
            item.resolution = {**resolution, "automatic": True}
    batch.created_count = len(parsed.candidates) - duplicate_count
    batch.duplicate_count = duplicate_count
    pending = db.scalar(
        select(func.count())
        .select_from(ImportReviewItem)
        .where(ImportReviewItem.batch_id == batch.id, ImportReviewItem.review_state == "pending")
    )
    batch.state = "open" if pending else "completed"
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type=actor.actor_type,
            actor_id=actor_id(actor),
            action="import.batch.created",
            aggregate_type="import_batch",
            aggregate_id=batch.id,
            details={
                "platform": parsed.platform,
                "created_count": batch.created_count,
                "duplicate_count": duplicate_count,
                "pending_review_count": pending or 0,
            },
        )
    )
    db.commit()
    return batch


@router.post("/imports/commit", status_code=201)
def imports_commit(
    data: ImportCommit,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.capture")),
    db: Session = Depends(get_db),
):
    if not idempotency_key:
        raise AppError(400, "idempotency_key_required", "必须提供 Idempotency-Key")
    if data.source:
        if data.source.format not in {"markdown", "csv"}:
            raise AppError(
                422, "unsupported_import_commit", "原始内容提交仅支持已识别表头的 Markdown 或 CSV"
            )
        try:
            parsed = parse_text_import(data.source.content, data.source.format)
        except ValueError as exc:
            raise AppError(422, "invalid_import", str(exc)) from exc
        ensure_categories(db, actor.owner_id)
        existing_batch = db.scalar(
            select(ImportBatch).where(
                ImportBatch.owner_id == actor.owner_id,
                ImportBatch.idempotency_key == idempotency_key,
            )
        )
        batch = _commit_markdown_import(
            db, actor, parsed, idempotency_key, jsonable(data.source.model_dump())
        )
        return _serialize_import_batch(db, batch, replayed=existing_batch is not None)
    source = db.scalar(
        select(CaptureSource).where(
            CaptureSource.owner_id == actor.owner_id,
            CaptureSource.source_type == "import",
            CaptureSource.source_external_id == (data.external_id or idempotency_key),
        )
    )
    if source:
        return {
            "record_ids": _existing_import_record_ids(db, source.id),
            "replayed": True,
        }
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
            db,
            actor.owner_id,
            record_data,
            f"{idempotency_key}:{index}",
            actor_id(actor),
            commit=False,
        )
        db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="import"))
        ids.append(str(record.id))
    db.commit()
    return {"record_ids": ids, "replayed": False}


@router.get("/import-reviews")
def import_reviews_list(
    state: str | None = Query(default=None, pattern="^(pending|resolved|dismissed)$"),
    limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    stmt = select(ImportReviewItem).where(ImportReviewItem.owner_id == actor.owner_id)
    if state:
        stmt = stmt.where(ImportReviewItem.review_state == state)
    rows = list(db.scalars(stmt.order_by(ImportReviewItem.created_at.desc()).limit(limit)))
    batch_ids = {row.batch_id for row in rows}
    batches = (
        {
            row.id: row
            for row in db.scalars(select(ImportBatch).where(ImportBatch.id.in_(batch_ids)))
        }
        if batch_ids
        else {}
    )
    return {
        "items": [
            {
                **serialize_review_item(row),
                "platform": batches[row.batch_id].platform,
            }
            for row in rows
        ],
        "quality": quality_metrics(db, actor.owner_id),
    }


@router.get("/import-batches/{batch_id}")
def import_batch_get(
    batch_id: uuid.UUID,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    batch = owned(db, ImportBatch, batch_id, actor.owner_id, "import_batch")
    return _serialize_import_batch(db, batch, replayed=False)


def _refresh_batch_state(db: Session, batch_id: uuid.UUID) -> None:
    pending = (
        db.scalar(
            select(func.count())
            .select_from(ImportReviewItem)
            .where(
                ImportReviewItem.batch_id == batch_id,
                ImportReviewItem.review_state == "pending",
            )
        )
        or 0
    )
    batch = db.get(ImportBatch, batch_id)
    if batch:
        batch.state = "open" if pending else "completed"


@router.post("/import-reviews/{review_id}/resolve")
def import_review_resolve(
    review_id: uuid.UUID,
    data: ImportReviewResolve,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "导入复核必须由用户确认")
    from app.services.review import resolve_review

    return resolve_review(db, actor.owner_id, review_id, data)


@router.get("/merchant-normalization-rules")
def merchant_rules_list(
    active: bool | None = None,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    stmt = select(MerchantNormalizationRule).where(
        MerchantNormalizationRule.owner_id == actor.owner_id
    )
    if active is not None:
        stmt = stmt.where(MerchantNormalizationRule.active == active)
    rows = db.scalars(stmt.order_by(MerchantNormalizationRule.updated_at.desc()).limit(500))
    return {
        "items": [
            simple_model(
                row,
                (
                    "id",
                    "match_kind",
                    "normalized_value",
                    "merchant_id",
                    "explanation",
                    "evidence_count",
                    "active",
                    "revision",
                    "revoked_at",
                ),
            )
            for row in rows
        ]
    }


@router.post("/merchant-normalization-rules/{rule_id}/revoke")
def merchant_rule_revoke(
    rule_id: uuid.UUID,
    data: RuleRevoke,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "规范化规则必须由用户会话撤销")
    rule = owned_locked(
        db, MerchantNormalizationRule, rule_id, actor.owner_id, "normalization_rule"
    )
    if rule.revision != data.revision:
        raise AppError(409, "revision_conflict", "规范化规则版本冲突")
    if not rule.active:
        raise AppError(409, "rule_already_revoked", "规范化规则已撤销")
    rule.active = False
    rule.revoked_at = datetime.now(UTC)
    rule.revision += 1
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type=actor.actor_type,
            actor_id=actor_id(actor),
            action="merchant.normalization_rule.revoked",
            aggregate_type="merchant_normalization_rule",
            aggregate_id=rule.id,
            details={"reason": data.reason},
        )
    )
    db.commit()
    return simple_model(
        rule,
        ("id", "merchant_id", "explanation", "evidence_count", "active", "revision", "revoked_at"),
    )


@router.get("/insights/data-quality")
def insight_data_quality(actor: Actor = Depends(user_actor), db: Session = Depends(get_db)):
    return quality_metrics(db, actor.owner_id)


@router.get("/merchants")
def merchants_list(
    query: str | None = None, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    actor: Actor = Depends(user_actor),
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
    merchant_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份整理需要用户会话")
    from app.services.identities import edit_identity

    edit_identity(db, actor.owner_id, "merchant", merchant_id, data, idempotency_key, alias=False)
    return merchants_get(merchant_id, actor, db)


def normalize(value: str) -> str:
    return "".join(value.casefold().split())


@router.post("/merchants/{merchant_id}/aliases", status_code=201)
def merchant_alias(
    merchant_id: uuid.UUID,
    data: AliasCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份整理需要用户会话")
    from app.services.identities import edit_identity

    row = edit_identity(
        db, actor.owner_id, "merchant", merchant_id, data, idempotency_key, alias=True
    )
    return simple_model(row, ("id", "alias", "normalized_alias", "source"))


@router.post("/merchants/{merchant_id}/merge")
def merchant_merge(
    merchant_id: uuid.UUID,
    data: MergeCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份合并必须由用户确认")
    from app.services.identities import merge_identity

    merge_identity(
        db, actor.owner_id, "merchant", merchant_id, data.target_id, data.reason, idempotency_key
    )
    return merchants_get(merchant_id, actor, db)


@router.get("/items")
def items_list(
    query: str | None = None, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    actor: Actor = Depends(user_actor),
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
    item_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份整理需要用户会话")
    from app.services.identities import edit_identity

    edit_identity(db, actor.owner_id, "item", item_id, data, idempotency_key, alias=False)
    return items_get(item_id, actor, db)


@router.post("/items/{item_id}/aliases", status_code=201)
def item_alias(
    item_id: uuid.UUID,
    data: AliasCreate,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份整理需要用户会话")
    from app.services.identities import edit_identity

    row = edit_identity(db, actor.owner_id, "item", item_id, data, idempotency_key, alias=True)
    return simple_model(row, ("id", "alias", "normalized_alias", "source"))


@router.post("/items/{item_id}/merge")
def item_merge(
    item_id: uuid.UUID,
    data: MergeCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份合并必须由用户确认")
    from app.services.identities import merge_identity

    merge_identity(
        db, actor.owner_id, "item", item_id, data.target_id, data.reason, idempotency_key
    )
    return items_get(item_id, actor, db)


@router.get("/identity-suggestions")
def suggestions(actor: Actor = Depends(user_actor), db: Session = Depends(get_db)):
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    if decision not in {"accept", "reject"}:
        raise AppError(404, "route_not_found", "操作不存在")
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "身份决定必须由用户确认")
    from app.services.identities import decide_suggestion

    row = decide_suggestion(db, actor.owner_id, suggestion_id, decision, idempotency_key)
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
    state: str | None = None, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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


def validate_planning_identities(db, owner_id, data):
    for field, model in (("merchant_id", Merchant), ("item_identity_id", ItemIdentity)):
        value = getattr(data, field, None)
        if value and not db.scalar(
            select(model.id).where(model.id == value, model.owner_id == owner_id)
        ):
            raise AppError(422, "planning_identity_not_found", "计划只能关联本人的商家或商品")


def planning_values(data):
    return {
        key: as_utc(value) if isinstance(value, datetime) else value
        for key, value in data.model_dump().items()
    }


@router.post("/intents", status_code=201)
def intents_create(
    data: IntentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    replay = replayed_create(
        db, actor, "intents.create", idempotency_key, data.model_dump(), SpendingIntent, "intent"
    )
    if replay:
        return simple_model(replay, INTENT_FIELDS)
    validate_planning_identities(db, actor.owner_id, data)
    row = SpendingIntent(owner_id=actor.owner_id, **planning_values(data))
    db.add(row)
    db.flush()
    remember_create(db, actor, "intents.create", idempotency_key or "", data.model_dump(), row.id)
    db.commit()
    return simple_model(row, INTENT_FIELDS)


@router.get("/intents/{intent_id}")
def intents_get(
    intent_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    row = owned_locked(db, SpendingIntent, intent_id, actor.owner_id, "intent")
    expected = parse_etag(if_match)
    if row.revision != expected:
        raise AppError(409, "revision_conflict", "计划已被更新")
    if row.state == "completed":
        raise AppError(409, "intent_completed", "已完成计划保留历史，不能重新打开")
    validate_planning_identities(db, actor.owner_id, data)
    for key, value in planning_values(data).items():
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
    actor: Actor = Depends(user_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
):
    payload = {"intent_id": str(intent_id)}
    replay = idempotency_lookup(db, actor.owner_id, "intent.draft", idempotency_key, payload)
    if replay:
        return serialize_record(db, get_record(db, actor.owner_id, replay.resource_id))
    row = owned_locked(db, SpendingIntent, intent_id, actor.owner_id, "intent")
    if row.state in {"completed", "cancelled", "skipped"}:
        raise AppError(409, "intent_inactive", "该计划已结束，不能生成新草稿")
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
    record = create_record(
        db,
        actor.owner_id,
        data,
        f"intent:{request_hash(idempotency_key).hex()}",
        actor_id(actor),
        commit=False,
    )
    idempotency_save(db, actor.owner_id, "intent.draft", idempotency_key, payload, record.id)
    db.commit()
    etag(response, record.revision)
    return serialize_record(db, record)


@router.post("/intents/{intent_id}/complete")
def intent_complete(
    intent_id: uuid.UUID,
    record_id: uuid.UUID,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    row = owned_locked(db, SpendingIntent, intent_id, actor.owner_id, "intent")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "计划已被更新")
    if row.state in {"completed", "cancelled", "skipped"}:
        raise AppError(409, "intent_inactive", "已结束计划不能重新关联消费")
    record = owned_locked(db, LedgerRecord, record_id, actor.owner_id, "record")
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
def commitments_list(actor: Actor = Depends(user_actor), db: Session = Depends(get_db)):
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
    actor: Actor = Depends(user_actor),
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
    validate_planning_identities(db, actor.owner_id, data)
    row = RecurringCommitment(owner_id=actor.owner_id, state="active", **planning_values(data))
    db.add(row)
    db.flush()
    remember_create(
        db, actor, "commitments.create", idempotency_key or "", data.model_dump(), row.id
    )
    db.commit()
    return simple_model(row, COMMITMENT_FIELDS)


@router.get("/recurring-commitments/{commitment_id}")
def commitments_get(
    commitment_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    row = owned_locked(db, RecurringCommitment, commitment_id, actor.owner_id, "commitment")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "周期事项已被更新")
    if state not in {"active", "paused", "ended"}:
        raise AppError(422, "invalid_state", "周期事项状态无效")
    validate_planning_identities(db, actor.owner_id, data)
    for key, value in planning_values(data).items():
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    payload = {
        "commitment_id": str(commitment_id),
        "occurrence": occurrence.isoformat() if occurrence else None,
    }
    if idempotency_key:
        replay = idempotency_lookup(
            db, actor.owner_id, "commitment.draft", idempotency_key, payload
        )
        if replay:
            return serialize_record(db, get_record(db, actor.owner_id, replay.resource_id))
    row = owned_locked(db, RecurringCommitment, commitment_id, actor.owner_id, "commitment")
    if occurrence is not None and occurrence.tzinfo is None:
        raise AppError(422, "timezone_required", "周期发生时间必须包含时区")
    occurrence = occurrence or row.next_due_at
    if occurrence.tzinfo is None:  # SQLite test storage; PostgreSQL retains the offset.
        occurrence = occurrence.replace(tzinfo=UTC)
    occurrence = occurrence.astimezone(UTC)
    key = f"commitment:{row.id}:occurrence:{occurrence.isoformat()}"
    existing = db.scalar(
        select(Reminder).where(Reminder.owner_id == actor.owner_id, Reminder.reminder_key == key)
    )
    if existing and existing.payload.get("record_id"):
        if idempotency_key:
            idempotency_save(
                db,
                actor.owner_id,
                "commitment.draft",
                idempotency_key,
                payload,
                uuid.UUID(existing.payload["record_id"]),
            )
            db.commit()
        return serialize_record(
            db, get_record(db, actor.owner_id, uuid.UUID(existing.payload["record_id"]))
        )
    if row.state != "active":
        raise AppError(409, "commitment_inactive", "暂停或已结束的周期不能生成新草稿")
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
    record = create_record(db, actor.owner_id, data, key, actor_id(actor), commit=False)
    reminder.payload = {**(reminder.payload or {}), "title": row.title, "record_id": str(record.id)}
    reminder.state = "handled"
    row.last_record_id = record.id
    row.revision += 1
    if idempotency_key:
        idempotency_save(
            db, actor.owner_id, "commitment.draft", idempotency_key, payload, record.id
        )
    db.commit()
    return serialize_record(db, record)


@router.get("/reminders")
def reminders_list(actor: Actor = Depends(user_actor), db: Session = Depends(get_db)):
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
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    states = {"read": "read", "dismiss": "dismissed"}
    if action not in states:
        raise AppError(404, "route_not_found", "操作不存在")
    row = owned_locked(db, Reminder, reminder_id, actor.owner_id, "reminder")
    row.state = states[action]
    db.commit()
    return simple_model(row, ("id", "state"))


USE_CYCLE_FIELDS = (
    "id",
    "item_identity_id",
    "source_record_id",
    "label",
    "started_at",
    "expected_end_at",
    "ended_at",
    "state",
    "note",
    "revision",
)


@router.get("/use-cycles")
def use_cycles_list(
    state: str | None = None,
    item_identity_id: uuid.UUID | None = None,
    actor: Actor = Depends(require_scope("ledger.read")),
    db: Session = Depends(get_db),
):
    statement = select(UseCycle).where(UseCycle.owner_id == actor.owner_id)
    if state:
        if state not in {"active", "completed", "cancelled"}:
            raise AppError(422, "invalid_state", "使用周期状态无效")
        statement = statement.where(UseCycle.state == state)
    if item_identity_id:
        statement = statement.where(UseCycle.item_identity_id == item_identity_id)
    rows = db.scalars(statement.order_by(UseCycle.started_at.desc(), UseCycle.id.desc()))
    return {"items": [simple_model(row, USE_CYCLE_FIELDS) for row in rows]}


@router.post("/use-cycles", status_code=201)
def use_cycles_create(
    data: UseCycleCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.confirm")),
    db: Session = Depends(get_db),
):
    owned(db, ItemIdentity, data.item_identity_id, actor.owner_id, "item")
    if data.source_record_id:
        source = get_record(db, actor.owner_id, data.source_record_id)
        if source.state != "confirmed":
            raise AppError(422, "confirmed_record_required", "使用周期只能引用已确认记录")
    replay = replayed_create(
        db,
        actor,
        "use-cycles.create",
        idempotency_key,
        data.model_dump(),
        UseCycle,
        "use_cycle",
    )
    if replay:
        etag(response, replay.revision)
        return simple_model(replay, USE_CYCLE_FIELDS)
    row = UseCycle(owner_id=actor.owner_id, state="active", **data.model_dump())
    db.add(row)
    db.flush()
    remember_create(
        db, actor, "use-cycles.create", idempotency_key or "", data.model_dump(), row.id
    )
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type=actor.actor_type,
            actor_id=actor_id(actor),
            action="use_cycle.started",
            aggregate_type="use_cycle",
            aggregate_id=row.id,
        )
    )
    db.commit()
    etag(response, row.revision)
    return simple_model(row, USE_CYCLE_FIELDS)


@router.patch("/use-cycles/{cycle_id}")
def use_cycles_patch(
    cycle_id: uuid.UUID,
    data: UseCyclePatch,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.confirm")),
    db: Session = Depends(get_db),
):
    row = owned(db, UseCycle, cycle_id, actor.owner_id, "use_cycle")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "使用周期已被更新")
    if row.state != "active":
        raise AppError(409, "invalid_state_transition", "只能修改进行中的使用周期")
    values = data.model_dump(exclude_unset=True)
    if values.get("expected_end_at") is not None and as_utc(values["expected_end_at"]) < as_utc(
        row.started_at
    ):
        raise AppError(422, "invalid_expected_end", "预计结束时间不能早于开始时间")
    for field, value in values.items():
        setattr(row, field, value)
    row.revision += 1
    db.commit()
    etag(response, row.revision)
    return simple_model(row, USE_CYCLE_FIELDS)


@router.post("/use-cycles/{cycle_id}/{action}")
def use_cycles_transition(
    cycle_id: uuid.UUID,
    action: str,
    data: UseCycleTransition,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.confirm")),
    db: Session = Depends(get_db),
):
    if action not in {"complete", "cancel"}:
        raise AppError(404, "route_not_found", "操作不存在")
    row = owned(db, UseCycle, cycle_id, actor.owner_id, "use_cycle")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "使用周期已被更新")
    if row.state != "active":
        raise AppError(409, "invalid_state_transition", "使用周期已经结束")
    ended_at = data.ended_at or datetime.now(UTC)
    if as_utc(ended_at) < as_utc(row.started_at):
        raise AppError(422, "invalid_end_time", "结束时间不能早于开始时间")
    row.state = "completed" if action == "complete" else "cancelled"
    row.ended_at = ended_at
    row.revision += 1
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type=actor.actor_type,
            actor_id=actor_id(actor),
            action=f"use_cycle.{row.state}",
            aggregate_type="use_cycle",
            aggregate_id=row.id,
        )
    )
    db.commit()
    etag(response, row.revision)
    return simple_model(row, USE_CYCLE_FIELDS)


@router.post("/forecasts/generate", status_code=201)
def forecasts_generate(
    data: ForecastGenerate,
    actor: Actor = Depends(require_scope("ledger.read")),
    db: Session = Depends(get_db),
):
    run, items, replayed = generate_forecast(
        db,
        actor.owner_id,
        data.as_of or datetime.now(ZoneInfo(data.timezone)).date(),
        data.timezone,
        data.horizon_days,
    )
    return decorate_run(db, run, items, serialize_run(run, items, replayed))


def _forecast_run(db: Session, owner_id: str, run_id: uuid.UUID) -> ForecastRun:
    row = db.scalar(
        select(ForecastRun).where(ForecastRun.id == run_id, ForecastRun.owner_id == owner_id)
    )
    if row is None:
        raise AppError(404, "forecast_not_found", "预测不存在")
    return row


def _forecast_response(db: Session, run: ForecastRun) -> dict[str, Any]:
    items = list(
        db.scalars(
            select(ForecastItem)
            .where(ForecastItem.run_id == run.id)
            .order_by(ForecastItem.predicted_at, ForecastItem.source_key)
        )
    )
    return decorate_run(db, run, items, serialize_run(run, items))


@router.get("/forecasts/latest")
def forecasts_latest(
    actor: Actor = Depends(require_scope("ledger.read")), db: Session = Depends(get_db)
):
    row = db.scalar(
        select(ForecastRun)
        .where(ForecastRun.owner_id == actor.owner_id)
        .order_by(ForecastRun.created_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )
    return {"run": _forecast_response(db, row) if row else None}


@router.get("/forecasts/{run_id}")
def forecasts_get(
    run_id: uuid.UUID,
    actor: Actor = Depends(require_scope("ledger.read")),
    db: Session = Depends(get_db),
):
    return _forecast_response(db, _forecast_run(db, actor.owner_id, run_id))


@router.post("/forecasts/{run_id}/verify")
def forecasts_verify(
    run_id: uuid.UUID,
    actor: Actor = Depends(require_scope("ledger.read")),
    db: Session = Depends(get_db),
):
    row = _forecast_run(db, actor.owner_id, run_id)
    return {
        "run_id": str(row.id),
        "algorithm_version": row.algorithm_version,
        "verified": verify_run(row),
    }


@router.post("/forecast-items/{item_id}/dismiss")
def forecast_item_dismiss(
    item_id: uuid.UUID,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(require_scope("ledger.write-draft")),
    db: Session = Depends(get_db),
):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "建议反馈需要用户会话")
    row = db.scalar(
        select(ForecastItem)
        .join(ForecastRun, ForecastItem.run_id == ForecastRun.id)
        .where(ForecastItem.id == item_id, ForecastRun.owner_id == actor.owner_id)
    )
    if row is None:
        raise AppError(404, "forecast_item_not_found", "预测项不存在")
    if row.revision != parse_etag(if_match):
        raise AppError(409, "revision_conflict", "预测项已被更新")
    row, feedback = save_feedback(
        db, actor.owner_id, item_id, parse_etag(if_match), None, "dismissed"
    )
    etag(response, row.revision)
    return {
        "id": str(row.id),
        "state": feedback.state,
        "revision": row.revision,
        "feedback_revision": feedback.revision,
    }


@router.post("/intake/webhooks/{adapter}", status_code=201)
def intake_webhook(
    adapter: str,
    data: StructuredIntake,
    actor: Actor = Depends(require_scope("ledger.capture")),
    db: Session = Depends(get_db),
):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,29}", adapter):
        raise AppError(422, "invalid_intake_adapter", "抓单适配器名称无效")
    return ingest_structured(
        db,
        actor.owner_id,
        "webhook",
        adapter,
        data,
        actor_id(actor),
        actor.actor_type,
    )


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
    month: date | None = None, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
):
    stmt = select(BudgetTarget).where(BudgetTarget.owner_id == actor.owner_id)
    if month:
        stmt = stmt.where(BudgetTarget.budget_month == month.replace(day=1))
    return {"items": [simple_model(row, BUDGET_FIELDS) for row in db.scalars(stmt)]}


@router.post("/budget-targets", status_code=201)
def budgets_create(
    data: BudgetCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
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
    budget_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
):
    return simple_model(owned(db, BudgetTarget, budget_id, actor.owner_id, "budget"), BUDGET_FIELDS)


@router.patch("/budget-targets/{budget_id}")
def budgets_patch(
    budget_id: uuid.UUID,
    data: BudgetCreate,
    response: Response,
    active: bool = True,
    if_match: str | None = Header(default=None, alias="If-Match"),
    actor: Actor = Depends(user_actor),
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


def month_range(month: str | None, timezone: str | None = None) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone or get_settings().default_timezone)
        start_date = (
            datetime.strptime(month, "%Y-%m").replace(tzinfo=zone)
            if month
            else datetime.now(zone).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        end = start_date + relativedelta(months=1)
    except (ValueError, KeyError, OverflowError) as exc:
        raise AppError(
            400, "invalid_month", "月份格式应为 YYYY-MM，时区应为有效 IANA 名称"
        ) from exc
    return start_date.astimezone(UTC), end.astimezone(UTC)


@router.get("/insights/summary")
def insight_summary(
    month: str | None = None,
    currency: str = "CNY",
    timezone: str | None = None,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    start, end = month_range(month, timezone)
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
    timezone: str | None = None,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    start, end = month_range(month, timezone)
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
    month: str | None = None,
    timezone: str | None = None,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    start, end = month_range(month, timezone)
    rows = db.execute(
        select(ConsumptionEvent.scene, func.count())
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            (MoneyEntry.type == "expense") | MoneyEntry.id.is_(None),
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
def insight_merchants(
    currency: str = "CNY", actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
):
    rows = db.execute(
        select(
            ConsumptionEvent.merchant_id,
            ConsumptionEvent.merchant_name_raw,
            func.sum(case(((MoneyEntry.type == "expense") | MoneyEntry.id.is_(None), 1), else_=0)),
            func.sum(
                case((MoneyEntry.type == "refund", -MoneyEntry.amount), else_=MoneyEntry.amount)
            ),
        )
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .outerjoin(MoneyEntry, ConsumptionEvent.money_entry_id == MoneyEntry.id)
        .where(
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            (MoneyEntry.currency == currency.upper()) | MoneyEntry.id.is_(None),
            MoneyEntry.type.in_(["expense", "refund"]) | MoneyEntry.id.is_(None),
        )
        .group_by(ConsumptionEvent.merchant_id, ConsumptionEvent.merchant_name_raw)
        .order_by(func.count().desc())
        .limit(50)
    ).all()
    return jsonable(
        {
            "currency": currency.upper(),
            "items": [
                {
                    "merchant_id": merchant_id,
                    "merchant_name_raw": name,
                    "count": count,
                    "amount": amount,
                }
                for merchant_id, name, count, amount in rows
            ],
        }
    )


@router.get("/insights/items")
def insight_items(actor: Actor = Depends(user_actor), db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            ConsumptionLine.item_identity_id,
            ConsumptionLine.raw_name,
            func.count(LedgerRecord.id.distinct()),
            func.max(LedgerRecord.occurred_at),
            func.min(LedgerRecord.occurred_at),
        )
        .select_from(ConsumptionLine)
        .join(ConsumptionEvent, ConsumptionLine.event_id == ConsumptionEvent.id)
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == actor.owner_id,
            LedgerRecord.state == "confirmed",
            (MoneyEntry.type == "expense") | MoneyEntry.id.is_(None),
        )
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
    month: str,
    timezone: str | None = None,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    start, end = month_range(month, timezone)
    budget_month = start.astimezone(ZoneInfo(timezone or get_settings().default_timezone)).date()
    targets = db.scalars(
        select(BudgetTarget).where(
            BudgetTarget.owner_id == actor.owner_id,
            BudgetTarget.budget_month == budget_month,
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
    record_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    row = owned(db, ExternalReference, reference_id, actor.owner_id, "reference")
    if row.source_type != "record" or row.source_id != record_id:
        raise AppError(404, "reference_not_found", "引用不存在")
    db.delete(row)
    db.commit()


@router.post("/records/{record_id}/archive-evidence", status_code=201)
def archive_evidence_create(
    record_id: uuid.UUID,
    data: ArchiveEvidenceCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(require_scope("ledger.integrations")),
    db: Session = Depends(get_db),
):
    get_record(db, actor.owner_id, record_id)
    binding = owned(db, AssetBinding, data.asset_binding_id, actor.owner_id, "asset_binding")
    if binding.released_at is not None:
        raise AppError(409, "asset_binding_released", "已释放的 Asset 引用不能交给 Archive")
    directly_bound = binding.source_type == "record" and binding.source_id == record_id
    source_bound = (
        binding.source_type == "capture_source"
        and db.scalar(
            select(LedgerRecordSource).where(
                LedgerRecordSource.record_id == record_id,
                LedgerRecordSource.source_id == binding.source_id,
            )
        )
        is not None
    )
    if not directly_bound and not source_bound:
        raise AppError(422, "asset_not_record_evidence", "Asset 引用不属于此消费记录")
    payload = {"record_id": record_id, **data.model_dump()}
    replay = replayed_create(
        db,
        actor,
        "archive_evidence.create",
        idempotency_key,
        payload,
        ArchiveEvidenceLink,
        "archive_evidence",
    )
    if replay:
        return simple_model(
            replay,
            ("id", "record_id", "asset_binding_id", "archive_uri", "active", "revision"),
        )
    row = ArchiveEvidenceLink(
        owner_id=actor.owner_id,
        record_id=record_id,
        asset_binding_id=binding.id,
        archive_uri=data.archive_uri,
    )
    db.add(row)
    db.flush()
    remember_create(db, actor, "archive_evidence.create", idempotency_key or "", payload, row.id)
    db.add(
        OutboxEvent(
            event_type="ledger.archive_evidence.linked",
            aggregate_type="record",
            aggregate_id=record_id,
            payload={
                "archive_uri": data.archive_uri,
                "asset_reference_id": (
                    str(binding.asset_reference_id) if binding.asset_reference_id else None
                ),
                "asset_id": str(binding.asset_id),
            },
        )
    )
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type=actor.actor_type,
            actor_id=actor_id(actor),
            action="archive.evidence.linked",
            aggregate_type="archive_evidence",
            aggregate_id=row.id,
            details={"record_id": str(record_id), "asset_binding_id": str(binding.id)},
        )
    )
    db.commit()
    return simple_model(
        row,
        ("id", "record_id", "asset_binding_id", "archive_uri", "active", "revision"),
    )


@router.get("/records/{record_id}/archive-evidence")
def archive_evidence_list(
    record_id: uuid.UUID,
    actor: Actor = Depends(user_actor),
    db: Session = Depends(get_db),
):
    get_record(db, actor.owner_id, record_id)
    rows = db.scalars(
        select(ArchiveEvidenceLink).where(
            ArchiveEvidenceLink.owner_id == actor.owner_id,
            ArchiveEvidenceLink.record_id == record_id,
        )
    )
    return {
        "items": [
            simple_model(
                row,
                (
                    "id",
                    "record_id",
                    "asset_binding_id",
                    "archive_uri",
                    "active",
                    "revision",
                    "released_at",
                ),
            )
            for row in rows
        ]
    }


@router.post("/records/{record_id}/archive-evidence/{link_id}/release")
def archive_evidence_release(
    record_id: uuid.UUID,
    link_id: uuid.UUID,
    data: ArchiveEvidenceRelease,
    actor: Actor = Depends(require_scope("ledger.integrations")),
    db: Session = Depends(get_db),
):
    row = owned_locked(db, ArchiveEvidenceLink, link_id, actor.owner_id, "archive_evidence")
    if row.record_id != record_id:
        raise AppError(404, "archive_evidence_not_found", "Archive 凭证引用不存在")
    if row.revision != data.revision:
        raise AppError(409, "revision_conflict", "Archive 凭证引用版本冲突")
    if not row.active:
        raise AppError(409, "archive_evidence_released", "Archive 凭证引用已释放")
    row.active = False
    row.released_at = datetime.now(UTC)
    row.revision += 1
    db.add(
        OutboxEvent(
            event_type="ledger.archive_evidence.released",
            aggregate_type="record",
            aggregate_id=record_id,
            payload={"archive_uri": row.archive_uri},
        )
    )
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type=actor.actor_type,
            actor_id=actor_id(actor),
            action="archive.evidence.released",
            aggregate_type="archive_evidence",
            aggregate_id=row.id,
            details={"reason": data.reason},
        )
    )
    db.commit()
    return simple_model(
        row,
        ("id", "record_id", "archive_uri", "active", "revision", "released_at"),
    )


@router.post("/exports", status_code=202)
def export_create(
    format: str = "json",
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: Actor = Depends(user_actor),
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
    export_id: uuid.UUID, actor: Actor = Depends(user_actor), db: Session = Depends(get_db)
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
