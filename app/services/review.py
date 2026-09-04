"""Shared user-review command; transport must provide a real user actor."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.models import (
    AuditEvent,
    ImportBatch,
    ImportReviewItem,
    LedgerRecord,
    Merchant,
    MerchantNormalizationRule,
)
from app.services.import_review import normalize_merchant, review_reasons, serialize_review_item
from app.services.records import _confirm_locked, get_record, record_query


def resolve_review(db, owner_id, review_id, data):
    initial = db.scalar(
        select(ImportReviewItem).where(
            ImportReviewItem.id == review_id, ImportReviewItem.owner_id == owner_id
        )
    )
    if initial is None:
        raise AppError(404, "import_review_not_found", "复核项不存在")
    record = db.scalar(
        record_query()
        .where(LedgerRecord.id == initial.record_id, LedgerRecord.owner_id == owner_id)
        .with_for_update(of=LedgerRecord)
    )
    item = db.scalar(
        select(ImportReviewItem)
        .where(ImportReviewItem.id == review_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item.revision != data.revision or (
        data.record_revision is not None and record.revision != data.record_revision
    ):
        raise AppError(409, "revision_conflict", "记录或复核项已更新，请刷新后重试")
    if item.review_state != "pending":
        raise AppError(409, "review_already_decided", "复核项已经处理")
    if record.state == "voided" and not data.dismiss:
        raise AppError(409, "record_voided", "已撤销记录只能关闭复核项")
    resolution = dict(item.resolution or {})
    changed = False
    if data.dismiss:
        item.review_state = "dismissed"
        resolution["dismissed"] = True
    else:
        if item.duplicate_of_record_id:
            raise AppError(
                409, "duplicate_review", "此项是重复来源，请关闭重复复核，不要再次确认事实"
            )
        if data.merchant_id:
            merchant = db.scalar(
                select(Merchant).where(
                    Merchant.id == data.merchant_id,
                    Merchant.owner_id == owner_id,
                    Merchant.active.is_(True),
                )
            )
            if merchant is None or record.consumption is None or not item.raw_merchant_name:
                raise AppError(422, "merchant_not_applicable", "请选择有效规范商家")
            record.consumption.merchant_id = merchant.id
            item.normalized_merchant_id = merchant.id
            resolution["merchant_id"] = str(merchant.id)
            changed = True
            if data.learn_merchant_rule:
                normalized = normalize_merchant(item.raw_merchant_name)
                rule = db.scalar(
                    select(MerchantNormalizationRule)
                    .where(
                        MerchantNormalizationRule.owner_id == owner_id,
                        MerchantNormalizationRule.normalized_value == normalized,
                        MerchantNormalizationRule.active.is_(True),
                    )
                    .with_for_update()
                )
                if rule and rule.merchant_id != merchant.id:
                    raise AppError(409, "normalization_rule_conflict", "请先撤销已有不同商家的规则")
                if rule:
                    rule.evidence_count += 1
                    rule.revision += 1
                else:
                    rule = MerchantNormalizationRule(
                        owner_id=owner_id,
                        normalized_value=normalized,
                        merchant_id=merchant.id,
                        explanation="用户确认的精确匹配规则",
                        source_review_item_id=item.id,
                        evidence_count=1,
                    )
                    db.add(rule)
                try:
                    db.flush()
                except IntegrityError as exc:
                    db.rollback()
                    raise AppError(
                        409, "normalization_rule_conflict", "规则已更新，请刷新"
                    ) from exc
                rule.explanation = (
                    f"由用户在 {rule.evidence_count} 条复核中确认；仅精确匹配原商家文本，可随时撤销"
                )
                resolution["normalization_rule_id"] = str(rule.id)
        if data.keep_merchant_unknown:
            resolution["merchant_unknown"] = True
        if data.refund_record_id:
            target = get_record(db, owner_id, data.refund_record_id)
            entry = record.money_entry
            if entry is None or entry.type != "refund":
                raise AppError(422, "not_a_refund", "此记录不是退款")
            if (
                target.id == record.id
                or target.state == "voided"
                or target.money_entry is None
                or target.money_entry.type != "expense"
                or target.money_entry.currency != entry.currency
            ):
                raise AppError(422, "invalid_refund_target", "请选择本人同币种的有效支出")
            entry.related_entry_id = target.money_entry.id
            item.refund_candidate_entry_id = target.money_entry.id
            resolution["refund_record_id"] = str(target.id)
            changed = True
        if data.keep_refund_unlinked:
            if record.money_entry is None or record.money_entry.type != "refund":
                raise AppError(422, "not_a_refund", "此记录不是退款")
            resolution["refund_unlinked"] = True
        if data.accept_amount_anomaly:
            if item.amount_anomaly_reason is None:
                raise AppError(422, "no_amount_anomaly", "没有待确认的金额异常")
            resolution["amount_anomaly_accepted"] = item.amount_anomaly_reason
            item.amount_anomaly_reason = None
        item.resolution = resolution
        if not review_reasons(item):
            item.review_state = "resolved"
        if changed:
            record.revision += 1
        db.flush()
        if data.confirm:
            _confirm_locked(db, record, owner_id)
    if data.note:
        resolution["note"] = data.note
    item.resolution = resolution
    item.revision += 1
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=owner_id,
            action="import.review.decided",
            aggregate_type="import_review",
            aggregate_id=item.id,
            details={
                "state": item.review_state,
                "record_changed": changed,
                "confirmed": data.confirm,
            },
        )
    )
    db.flush()
    pending = db.scalar(
        select(func.count())
        .select_from(ImportReviewItem)
        .where(
            ImportReviewItem.batch_id == item.batch_id, ImportReviewItem.review_state == "pending"
        )
    )
    db.get(ImportBatch, item.batch_id).state = "open" if pending else "completed"
    db.commit()
    return {
        **serialize_review_item(item),
        "record_revision": record.revision,
        "record_state": record.state,
    }
