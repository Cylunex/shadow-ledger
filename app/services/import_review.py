from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers import ImportCandidate
from app.models import (
    ConsumptionEvent,
    ImportReviewItem,
    LedgerRecord,
    MerchantNormalizationRule,
    MoneyEntry,
)
from app.schemas import RecordCreate, jsonable


def normalize_merchant(value: str) -> str:
    return "".join(value.casefold().split())


def active_rule(
    db: Session, owner_id: str, raw_merchant_name: str | None
) -> MerchantNormalizationRule | None:
    if not raw_merchant_name:
        return None
    return db.scalar(
        select(MerchantNormalizationRule).where(
            MerchantNormalizationRule.owner_id == owner_id,
            MerchantNormalizationRule.normalized_value == normalize_merchant(raw_merchant_name),
            MerchantNormalizationRule.active.is_(True),
        )
    )


def apply_normalization_rule(
    db: Session, owner_id: str, candidate: ImportCandidate
) -> tuple[RecordCreate, MerchantNormalizationRule | None]:
    consumption = candidate.record.consumption
    rule = active_rule(db, owner_id, consumption.merchant_name_raw if consumption else None)
    if rule is None or consumption is None:
        return candidate.record, None
    # Only the canonical association changes. Imported payee and item text remain untouched.
    normalized = consumption.model_copy(update={"merchant_id": rule.merchant_id}, deep=True)
    return candidate.record.model_copy(update={"consumption": normalized}, deep=True), rule


def amount_anomaly_reason(db: Session, owner_id: str, record: RecordCreate) -> str | None:
    entry = record.money_entry
    if entry is None or entry.type != "expense":
        return None
    consumption = record.consumption
    stmt = (
        select(MoneyEntry.amount)
        .join(LedgerRecord, LedgerRecord.id == MoneyEntry.record_id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == owner_id,
            LedgerRecord.state == "confirmed",
            MoneyEntry.type == "expense",
            MoneyEntry.currency == entry.currency,
        )
    )
    if consumption and consumption.merchant_id:
        stmt = stmt.where(ConsumptionEvent.merchant_id == consumption.merchant_id)
    elif consumption and consumption.merchant_name_raw:
        stmt = stmt.where(
            func.lower(ConsumptionEvent.merchant_name_raw)
            == consumption.merchant_name_raw.casefold()
        )
    elif entry.title:
        stmt = stmt.where(func.lower(MoneyEntry.title) == entry.title.casefold())
    else:
        return None
    samples = [
        Decimal(value)
        for value in db.scalars(
            stmt.order_by(LedgerRecord.occurred_at.desc(), LedgerRecord.id.desc()).limit(101)
        )
    ]
    if len(samples) < 3:
        return None
    median = Decimal(str(statistics.median(samples)))
    if entry.amount > median * Decimal("3"):
        return "above_3x_historical_median"
    if entry.amount * Decimal("3") < median:
        return "below_one_third_historical_median"
    return None


def refund_candidate(
    db: Session, owner_id: str, record: RecordCreate, payload=None
) -> MoneyEntry | None:
    import uuid

    from app.services.matching import refund_candidates

    rows = refund_candidates(db, owner_id, record, payload=payload, limit=1)
    return db.get(MoneyEntry, uuid.UUID(rows[0]["entry_id"])) if rows else None


def review_reasons(item: ImportReviewItem) -> list[str]:
    reasons: list[str] = []
    if item.duplicate_of_record_id:
        reasons.append("duplicate")
    if item.resolution.get("refund_record_id") or item.resolution.get("refund_unlinked"):
        pass
    elif item.refund_candidate_entry_id:
        reasons.append("refund_match_suggested")
    elif item.resolution.get("money_type") == "refund":
        reasons.append("refund_match_missing")
    if item.amount_anomaly_reason:
        reasons.append("amount_anomaly")
    if (
        item.raw_merchant_name
        and item.normalized_merchant_id is None
        and not item.resolution.get("merchant_unknown")
    ):
        reasons.append("merchant_confirmation_needed")
    return reasons


def serialize_review_item(item: ImportReviewItem) -> dict[str, Any]:
    return jsonable(
        {
            "id": item.id,
            "batch_id": item.batch_id,
            "record_id": item.record_id,
            "source_id": item.source_id,
            "source_external_id": item.source_external_id,
            "raw_merchant_name": item.raw_merchant_name,
            "raw_item_names": item.raw_item_names,
            "normalized_merchant_id": item.normalized_merchant_id,
            "duplicate_of_record_id": item.duplicate_of_record_id,
            "refund_candidate_entry_id": item.refund_candidate_entry_id,
            "amount_anomaly_reason": item.amount_anomaly_reason,
            "review_reasons": review_reasons(item),
            "review_state": item.review_state,
            "resolution": item.resolution,
            "revision": item.revision,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
    )


def quality_metrics(db: Session, owner_id: str) -> dict[str, Any]:
    events = list(
        db.execute(
            select(ConsumptionEvent, MoneyEntry)
            .join(LedgerRecord, LedgerRecord.id == ConsumptionEvent.record_id)
            .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
            .where(LedgerRecord.owner_id == owner_id, LedgerRecord.state == "confirmed")
        )
    )
    total = len(events)
    with_amount = sum(entry is not None for _, entry in events)
    with_raw_merchant = sum(bool(event.merchant_name_raw) for event, _ in events)
    with_canonical_merchant = sum(event.merchant_id is not None for event, _ in events)
    pending_reviews = (
        db.scalar(
            select(func.count())
            .select_from(ImportReviewItem)
            .where(
                ImportReviewItem.owner_id == owner_id,
                ImportReviewItem.review_state == "pending",
            )
        )
        or 0
    )

    def ratio(value: int) -> str:
        return format(Decimal(value) / Decimal(total), ".4f") if total else "0.0000"

    sample_count = total
    blockers: list[str] = []
    if sample_count < 12:
        blockers.append("fewer_than_12_confirmed_consumption_facts")
    if total and Decimal(with_amount) / Decimal(total) < Decimal("0.80"):
        blockers.append("amount_completeness_below_80_percent")
    if total and Decimal(with_canonical_merchant) / Decimal(total) < Decimal("0.60"):
        blockers.append("merchant_identity_coverage_below_60_percent")
    if pending_reviews:
        blockers.append("pending_import_reviews")
    return {
        "data_completeness": {
            "confirmed_consumption_facts": total,
            "amount_coverage": ratio(with_amount),
            "raw_merchant_coverage": ratio(with_raw_merchant),
            "canonical_merchant_coverage": ratio(with_canonical_merchant),
            "pending_import_reviews": pending_reviews,
        },
        "prediction_readiness": {
            "status": "ready_for_evaluation" if not blockers else "insufficient",
            "sample_count": sample_count,
            "blockers": blockers,
            "creates_forecast": False,
            "note": "Readiness is descriptive only; deterministic Forecast runs separately and never creates facts.",
        },
    }
