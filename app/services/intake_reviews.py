from app.importers import ImportCandidate
from app.models import ImportBatch, ImportReviewItem
from app.services.import_review import (
    amount_anomaly_reason,
    apply_normalization_rule,
    refund_candidate,
    review_reasons,
)
from app.services.records import request_hash


def register_intake_reviews(db, owner_id, source, records, candidates):
    batch = ImportBatch(
        owner_id=owner_id,
        idempotency_key=f"intake:{source.id}",
        request_hash=request_hash(source.raw_payload),
        platform="structured",
        row_count=len(records),
        created_count=len(records),
        state="completed",
    )
    db.add(batch)
    db.flush()
    for record, candidate in zip(records, candidates, strict=True):
        normalized, rule = apply_normalization_rule(
            db, owner_id, ImportCandidate(candidate, source.source_external_id, source.raw_payload)
        )
        if rule and record.consumption:
            record.consumption.merchant_id = rule.merchant_id
        event = candidate.consumption
        refund = refund_candidate(db, owner_id, normalized)
        item = ImportReviewItem(
            owner_id=owner_id,
            batch_id=batch.id,
            source_id=source.id,
            record_id=record.id,
            source_external_id=source.source_external_id or str(source.id),
            raw_merchant_name=event.merchant_name_raw if event else None,
            raw_item_names=[line.raw_name for line in event.lines] if event else [],
            normalized_merchant_id=rule.merchant_id
            if rule
            else (event.merchant_id if event else None),
            refund_candidate_entry_id=refund.id if refund else None,
            amount_anomaly_reason=amount_anomaly_reason(db, owner_id, normalized),
            resolution={
                "money_type": candidate.money_entry.type if candidate.money_entry else None
            },
        )
        db.add(item)
        if review_reasons(item):
            item.review_state = "pending"
            batch.state = "open"
        else:
            item.review_state = "resolved"
