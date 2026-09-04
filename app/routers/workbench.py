from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import func, select

from app.db import get_db
from app.errors import AppError
from app.models import (
    ArchiveEvidenceLink,
    AssetBinding,
    AuditEvent,
    CaptureSource,
    ConsumptionEvent,
    ConsumptionLine,
    ExternalReference,
    IdentitySuggestion,
    ImportBatch,
    ImportReviewItem,
    ItemIdentity,
    LedgerRecord,
    LedgerRecordSource,
    Merchant,
    MoneyEntry,
    RecurringCommitment,
    SourceObservation,
    SpendingIntent,
    UseCycle,
)
from app.schemas import RecordCreate, jsonable
from app.security import Actor, current_actor, require_scope
from app.services.feedback import effective_feedback, save_feedback
from app.services.identities import family_ids
from app.services.import_review import serialize_review_item
from app.services.matching import refund_candidates
from app.services.records import (
    get_record,
    idempotency_lookup,
    idempotency_save,
    record_query,
    serialize_record,
)
from app.services.sources import (
    append_observation,
    decide_observation,
    ensure_baseline,
    serialize_observation,
)
from app.services.workbench import pending_work
from app.workbench_schemas import (
    EvidenceLinkInput,
    FeedbackInput,
    IdentityAssignment,
    ObservationDecision,
    ObservationInput,
)

router = APIRouter(prefix="/api/v1", tags=["consumption-workbench"])


def user_actor(actor: Actor = Depends(current_actor)):
    if actor.actor_type != "user":
        raise AppError(403, "user_confirmation_required", "此操作只允许用户会话")
    return actor


@router.get("/forecast-evaluation")
def forecast_backtest(
    as_of: date,
    timezone: str = Query("Asia/Shanghai", max_length=64),
    horizon_days: int = Query(90, ge=1, le=365),
    actor=Depends(user_actor),
    db=Depends(get_db),
):
    from app.services.backtest import backtest_repeat

    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise AppError(422, "invalid_timezone", "时区无效") from exc
    return backtest_repeat(db, actor.owner_id, as_of, timezone, horizon_days)


def owned(db, model, identity, owner):
    row = db.scalar(select(model).where(model.id == identity, model.owner_id == owner))
    if row is None:
        raise AppError(404, "resource_not_found", "资源不存在")
    return row


@router.get("/workbench")
def workbench(
    kind: str | None = Query(None, pattern="^(draft|review|observation|failed|identity)$"),
    query: str | None = Query(None, max_length=200),
    cursor: str | None = Query(None, max_length=1000),
    limit: int = Query(30, ge=1, le=100),
    actor=Depends(user_actor),
    db=Depends(get_db),
):
    return pending_work(db, actor.owner_id, kind, query, cursor, limit)


@router.get("/import-reviews/{review_id}")
def review_detail(review_id: UUID, actor=Depends(user_actor), db=Depends(get_db)):
    row = owned(db, ImportReviewItem, review_id, actor.owner_id)
    record = get_record(db, actor.owner_id, row.record_id)
    return {
        **serialize_review_item(row),
        "record": serialize_record(db, record),
        "source_id": str(row.source_id),
    }


@router.get("/identity-suggestions/{suggestion_id}")
def identity_suggestion(suggestion_id: UUID, actor=Depends(user_actor), db=Depends(get_db)):
    row = owned(db, IdentitySuggestion, suggestion_id, actor.owner_id)
    model = Merchant if row.source_type == "merchant" else ItemIdentity
    source = owned(db, model, row.source_id, actor.owner_id)
    target = owned(db, model, row.target_id, actor.owner_id)
    return {
        "id": str(row.id),
        "state": row.state,
        "kind": row.source_type,
        "source": source.canonical_name,
        "target": target.canonical_name,
        "source_id": str(source.id),
        "target_id": str(target.id),
        "reason": row.reason,
    }


@router.post("/identities/propose")
def identities_propose(
    key: str | None = Header(None, alias="Idempotency-Key"),
    actor=Depends(user_actor),
    db=Depends(get_db),
):
    from app.services.identities import propose_identities

    replay = idempotency_lookup(db, actor.owner_id, "identities.propose", key, {})
    if replay:
        return {"replayed": True}
    count = propose_identities(db, actor.owner_id)
    idempotency_save(db, actor.owner_id, "identities.propose", key, {}, None)
    db.commit()
    return {"created": count, "replayed": False, "scan_limit_per_kind": 500}


@router.get("/records/{record_id}/refund-candidates")
def refunds(
    record_id: UUID,
    query: str | None = Query(None, max_length=200),
    actor=Depends(user_actor),
    db=Depends(get_db),
):
    record = get_record(db, actor.owner_id, record_id)
    data = serialize_record(db, record)
    candidate = (
        RecordCreate.model_validate(
            {key: data[key] for key in ("occurred_at", "timezone", "note")}
            | {
                "money_entry": {
                    key: value for key, value in (data["money_entry"] or {}).items() if key != "id"
                }
                or None,
                "consumption": None,
            }
        )
        if record.money_entry
        else None
    )
    if candidate is None:
        return {"items": [], "window_days": 180}
    if record.consumption:
        from app.schemas import ConsumptionInput

        candidate.consumption = ConsumptionInput(
            scene=record.consumption.scene,
            merchant_id=record.consumption.merchant_id,
            merchant_name_raw=record.consumption.merchant_name_raw,
        )
    source = db.scalar(
        select(CaptureSource)
        .join(LedgerRecordSource, LedgerRecordSource.source_id == CaptureSource.id)
        .where(LedgerRecordSource.record_id == record.id, CaptureSource.owner_id == actor.owner_id)
        .order_by(CaptureSource.created_at, CaptureSource.id)
        .limit(1)
    )
    return {
        "items": refund_candidates(
            db,
            actor.owner_id,
            candidate,
            payload=source.raw_payload if source else None,
            query=query,
        ),
        "window_days": 180,
        "notice": "候选仅用于人工选择；弱匹配不代表同一消费。",
    }


@router.get("/records/{record_id}/evidence")
def evidence(record_id: UUID, actor=Depends(user_actor), db=Depends(get_db)):
    record = get_record(db, actor.owner_id, record_id)
    sources = list(
        db.scalars(
            select(CaptureSource)
            .join(LedgerRecordSource, LedgerRecordSource.source_id == CaptureSource.id)
            .where(
                LedgerRecordSource.record_id == record.id, CaptureSource.owner_id == actor.owner_id
            )
            .order_by(CaptureSource.created_at, CaptureSource.id)
            .limit(101)
        )
    )
    audits = list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.owner_id == actor.owner_id,
                AuditEvent.aggregate_type == "record",
                AuditEvent.aggregate_id == record.id,
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(30)
        )
    )
    refunds = (
        list(
            db.execute(
                select(MoneyEntry, LedgerRecord)
                .join(LedgerRecord, LedgerRecord.id == MoneyEntry.record_id)
                .where(
                    LedgerRecord.owner_id == actor.owner_id,
                    MoneyEntry.related_entry_id == record.money_entry.id,
                    MoneyEntry.type == "refund",
                    LedgerRecord.state != "voided",
                )
                .order_by(LedgerRecord.occurred_at.desc())
                .limit(100)
            )
        )
        if record.money_entry
        else []
    )
    return jsonable(
        {
            "sources": [
                {
                    "id": row.id,
                    "type": row.source_type,
                    "parser": row.parser,
                    "parser_version": row.parser_version,
                    "state": row.capture_state,
                    "raw_text": row.raw_text,
                    "raw_payload": row.raw_payload,
                }
                for row in sources[:100]
            ],
            "sources_truncated": len(sources) > 100,
            "audit": [
                {"action": row.action, "at": row.occurred_at, "details": row.details}
                for row in audits
            ],
            "refunds": [
                {
                    "record_id": row.id,
                    "amount": money.amount,
                    "currency": money.currency,
                    "state": row.state,
                    "occurred_at": row.occurred_at,
                }
                for money, row in refunds
            ],
        }
    )


@router.get("/capture-sources/{source_id}/observations")
def observations(
    source_id: UUID, offset: int = Query(0, ge=0), actor=Depends(user_actor), db=Depends(get_db)
):
    source = owned(db, CaptureSource, source_id, actor.owner_id)
    rows = list(
        db.scalars(
            select(SourceObservation)
            .where(SourceObservation.source_id == source.id)
            .order_by(SourceObservation.created_at.desc(), SourceObservation.id.desc())
            .offset(offset)
            .limit(21)
        )
    )
    return {
        "items": [serialize_observation(row) for row in rows[:20]],
        "next_offset": offset + 20 if len(rows) > 20 else None,
    }


@router.post("/capture-sources/{source_id}/observations", status_code=201)
def observation_create(
    source_id: UUID,
    data: ObservationInput,
    actor=Depends(require_scope("ledger.capture")),
    db=Depends(get_db),
):
    source = owned(db, CaptureSource, source_id, actor.owner_id)
    ensure_baseline(db, source)
    row, replayed = append_observation(
        db,
        source,
        data.external_revision,
        data.payload,
        data.candidate,
        parser=data.parser,
        parser_version=data.parser_version,
    )
    db.commit()
    return {**serialize_observation(row), "replayed": replayed}


@router.get("/source-observations/{observation_id}")
def observation_detail(observation_id: UUID, actor=Depends(user_actor), db=Depends(get_db)):
    row = owned(db, SourceObservation, observation_id, actor.owner_id)
    linked = list(
        db.scalars(
            select(LedgerRecord)
            .join(LedgerRecordSource, LedgerRecordSource.record_id == LedgerRecord.id)
            .where(
                LedgerRecordSource.source_id == row.source_id,
                LedgerRecord.owner_id == actor.owner_id,
            )
            .limit(50)
        )
    )
    return {
        **serialize_observation(row),
        "records": [
            serialize_record(db, get_record(db, actor.owner_id, record.id)) for record in linked
        ],
    }


@router.post("/source-observations/{observation_id}/decide")
def observation_decide(
    observation_id: UUID, data: ObservationDecision, actor=Depends(user_actor), db=Depends(get_db)
):
    return decide_observation(db, actor.owner_id, observation_id, data)


@router.post("/forecast-items/{item_id}/feedback")
def feedback(item_id: UUID, data: FeedbackInput, actor=Depends(user_actor), db=Depends(get_db)):
    item, row = save_feedback(
        db,
        actor.owner_id,
        item_id,
        data.revision,
        data.feedback_revision,
        data.state,
        data.snoozed_until,
    )
    return {
        "id": str(item.id),
        "revision": item.revision,
        **effective_feedback(item, {row.episode_key: row}),
    }


@router.post("/records/{record_id}/link-evidence")
def link_evidence(
    record_id: UUID,
    data: EvidenceLinkInput,
    key: str | None = Header(None, alias="Idempotency-Key"),
    actor=Depends(user_actor),
    db=Depends(get_db),
):
    payload = {"record_id": str(record_id), **jsonable(data.model_dump())}
    replay = idempotency_lookup(db, actor.owner_id, "evidence.link", key, payload)
    if replay:
        return {"record_id": str(replay.resource_id), "replayed": True}
    source = owned(db, CaptureSource, data.source_id, actor.owner_id)
    ids = [record_id] + ([data.redundant_draft_id] if data.redundant_draft_id else [])
    rows = {
        row.id: row
        for row in db.scalars(
            record_query()
            .where(LedgerRecord.owner_id == actor.owner_id, LedgerRecord.id.in_(ids))
            .order_by(LedgerRecord.id)
            .with_for_update(of=LedgerRecord)
        )
    }
    target = rows.get(record_id)
    if target is None:
        raise AppError(404, "record_not_found", "目标记录不存在")
    if target.revision != data.record_revision or target.state == "voided":
        raise AppError(409, "revision_conflict", "目标已更新或撤销")
    duplicate = None
    if data.redundant_draft_id:
        duplicate = rows.get(data.redundant_draft_id)
        if (
            duplicate is None
            or duplicate.id == target.id
            or duplicate.state != "draft"
            or duplicate.revision != data.redundant_revision
        ):
            raise AppError(409, "invalid_duplicate_draft", "仅可合并版本一致的其他草稿")
        link = db.scalar(
            select(LedgerRecordSource).where(
                LedgerRecordSource.record_id == duplicate.id,
                LedgerRecordSource.source_id == source.id,
            )
        )
        if link is None:
            raise AppError(422, "source_not_linked", "来源不属于此草稿")
        # Asset bindings encode external ownership. Never silently move them to another object.
        checks = [
            select(AssetBinding.id).where(
                AssetBinding.source_type == "record", AssetBinding.source_id == duplicate.id
            ),
            select(ExternalReference.id).where(
                ExternalReference.source_type == "record",
                ExternalReference.source_id == duplicate.id,
            ),
            select(ArchiveEvidenceLink.id).where(ArchiveEvidenceLink.record_id == duplicate.id),
            select(UseCycle.id).where(UseCycle.source_record_id == duplicate.id),
            select(RecurringCommitment.id).where(
                RecurringCommitment.last_record_id == duplicate.id
            ),
            select(SpendingIntent.id).where(SpendingIntent.completed_record_id == duplicate.id),
        ]
        if duplicate.money_entry:
            checks.append(
                select(MoneyEntry.id).where(MoneyEntry.related_entry_id == duplicate.money_entry.id)
            )
            checks.append(
                select(ImportReviewItem.id).where(
                    ImportReviewItem.refund_candidate_entry_id == duplicate.money_entry.id
                )
            )
        if any(db.scalar(stmt.limit(1)) for stmt in checks):
            raise AppError(
                409,
                "duplicate_has_references",
                "草稿有附件或其他业务引用，请先处理引用；未删除任何内容",
            )
        affected_batches = set()
        for review in db.scalars(
            select(ImportReviewItem).where(
                ImportReviewItem.owner_id == actor.owner_id,
                (ImportReviewItem.record_id == duplicate.id)
                | (ImportReviewItem.duplicate_of_record_id == duplicate.id),
            )
        ):
            affected_batches.add(review.batch_id)
            review.record_id = target.id
            review.duplicate_of_record_id = target.id
            review.review_state = "dismissed"
            review.revision += 1
            review.resolution = {**review.resolution, "evidence_merged": True}
        for link in list(
            db.scalars(
                select(LedgerRecordSource).where(LedgerRecordSource.record_id == duplicate.id)
            )
        ):
            present = db.scalar(
                select(LedgerRecordSource).where(
                    LedgerRecordSource.record_id == target.id,
                    LedgerRecordSource.source_id == link.source_id,
                    LedgerRecordSource.role == link.role,
                )
            )
            if not present:
                db.add(
                    LedgerRecordSource(
                        record_id=target.id, source_id=link.source_id, role=link.role
                    )
                )
            db.delete(link)
        db.flush()
        for batch_id in affected_batches:
            batch = db.get(ImportBatch, batch_id)
            pending = db.scalar(
                select(ImportReviewItem.id)
                .where(
                    ImportReviewItem.batch_id == batch_id,
                    ImportReviewItem.review_state == "pending",
                )
                .limit(1)
            )
            batch.state = "open" if pending else "completed"
        duplicate.consumption = None
        db.flush()
        db.delete(duplicate)
    present = db.scalar(
        select(LedgerRecordSource).where(
            LedgerRecordSource.record_id == target.id, LedgerRecordSource.source_id == source.id
        )
    )
    if not present:
        db.add(LedgerRecordSource(record_id=target.id, source_id=source.id, role="evidence"))
    target.revision += 1
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type="user",
            actor_id=actor.owner_id,
            action="record.evidence.linked",
            aggregate_type="record",
            aggregate_id=target.id,
            details={
                "source_id": str(source.id),
                "removed_draft_id": str(duplicate.id) if duplicate else None,
                "reason": data.reason,
            },
        )
    )
    idempotency_save(db, actor.owner_id, "evidence.link", key, payload, target.id)
    db.commit()
    return {
        "record_id": str(target.id),
        "revision": target.revision,
        "replayed": False,
        "removed_draft_id": str(duplicate.id) if duplicate else None,
    }


@router.post("/records/{record_id}/identities")
def assign_identities(
    record_id: UUID, data: IdentityAssignment, actor=Depends(user_actor), db=Depends(get_db)
):
    record = db.scalar(
        record_query()
        .where(LedgerRecord.id == record_id, LedgerRecord.owner_id == actor.owner_id)
        .with_for_update(of=LedgerRecord)
    )
    if record is None:
        raise AppError(404, "record_not_found", "记录不存在")
    if record.state == "voided" or record.revision != data.revision:
        raise AppError(409, "revision_conflict", "记录已经更新或撤销")
    if record.consumption is None:
        raise AppError(422, "no_consumption", "此记录没有消费明细")
    if data.merchant_id:
        merchant = owned(db, Merchant, data.merchant_id, actor.owner_id)
        if not merchant.active:
            raise AppError(409, "identity_inactive", "商家已停用或合并")
        record.consumption.merchant_id = merchant.id
    lines = {line.id: line for line in record.consumption.lines}
    for line_id, item_id in data.lines.items():
        item = owned(db, ItemIdentity, item_id, actor.owner_id)
        if line_id not in lines or not item.active:
            raise AppError(422, "invalid_line_identity", "请选择本条明细与有效商品身份")
        lines[line_id].item_identity_id = item.id
    record.revision += 1
    db.add(
        AuditEvent(
            owner_id=actor.owner_id,
            actor_type="user",
            actor_id=actor.owner_id,
            action="record.identities.assigned",
            aggregate_type="record",
            aggregate_id=record.id,
            details={
                "line_count": len(data.lines),
                "merchant_linked": data.merchant_id is not None,
            },
        )
    )
    db.commit()
    return serialize_record(db, get_record(db, actor.owner_id, record.id))


@router.get("/identities/{kind}/{identity_id}/memory")
def memory(
    kind: str,
    identity_id: UUID,
    offset: int = Query(0, ge=0),
    actor=Depends(user_actor),
    db=Depends(get_db),
):
    if kind not in {"merchant", "item"}:
        raise AppError(422, "invalid_identity_kind", "身份类型无效")
    root, ids = family_ids(db, actor.owner_id, kind, identity_id)
    model = Merchant if kind == "merchant" else ItemIdentity
    identity = owned(db, model, root, actor.owner_id)
    stmt = (
        select(LedgerRecord.id)
        .join(ConsumptionEvent)
        .where(LedgerRecord.owner_id == actor.owner_id, LedgerRecord.state == "confirmed")
    )
    if kind == "merchant":
        stmt = stmt.where(ConsumptionEvent.merchant_id.in_(ids))
    else:
        stmt = stmt.where(
            select(ConsumptionLine.id)
            .where(
                ConsumptionLine.event_id == ConsumptionEvent.id,
                ConsumptionLine.item_identity_id.in_(ids),
            )
            .exists()
        )
    count = db.scalar(select(func.count()).select_from(stmt.subquery()))
    record_ids = list(
        db.scalars(
            stmt.order_by(LedgerRecord.occurred_at.desc(), LedgerRecord.id.desc())
            .offset(offset)
            .limit(21)
        )
    )
    cycles = (
        list(
            db.scalars(
                select(UseCycle)
                .where(UseCycle.owner_id == actor.owner_id, UseCycle.item_identity_id.in_(ids))
                .order_by(UseCycle.started_at.desc())
                .limit(30)
            )
        )
        if kind == "item"
        else []
    )
    return jsonable(
        {
            "identity": {
                "id": identity.id,
                "name": identity.canonical_name,
                "brand": getattr(identity, "brand", None),
                "variant": getattr(identity, "variant", None),
            },
            "count": count,
            "items": [
                serialize_record(db, get_record(db, actor.owner_id, rid)) for rid in record_ids[:20]
            ],
            "next_offset": offset + 20 if len(record_ids) > 20 else None,
            "use_cycles": [
                {
                    "id": row.id,
                    "state": row.state,
                    "started_at": row.started_at,
                    "ended_at": row.ended_at,
                    "expected_end_at": row.expected_end_at,
                }
                for row in cycles
            ],
            "price_notice": "展示整单金额；不把多商品整单价当商品单价。",
        }
    )
