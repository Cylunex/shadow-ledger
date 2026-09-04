import base64
import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, literal, or_, select, union_all

from app.errors import AppError
from app.models import (
    CaptureSource,
    ConsumptionEvent,
    IdentitySuggestion,
    ImportReviewItem,
    LedgerRecord,
    MoneyEntry,
    SourceObservation,
)


def pending_work(db, owner_id, kind=None, query=None, cursor=None, limit=30):
    drafts = (
        select(
            LedgerRecord.id,
            literal("draft").label("kind"),
            LedgerRecord.created_at.label("created_at"),
            LedgerRecord.id.label("target_id"),
            func.coalesce(
                func.nullif(MoneyEntry.title, ""),
                ConsumptionEvent.merchant_name_raw,
                literal("待确认草稿"),
            ).label("title"),
        )
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == owner_id,
            LedgerRecord.state == "draft",
            ~select(ImportReviewItem.id)
            .where(
                ImportReviewItem.record_id == LedgerRecord.id,
                ImportReviewItem.review_state == "pending",
            )
            .exists(),
        )
    )
    reviews = select(
        ImportReviewItem.id,
        literal("review"),
        ImportReviewItem.created_at,
        ImportReviewItem.record_id,
        func.coalesce(ImportReviewItem.raw_merchant_name, literal("导入复核")),
    ).where(ImportReviewItem.owner_id == owner_id, ImportReviewItem.review_state == "pending")
    observations = select(
        SourceObservation.id,
        literal("observation"),
        SourceObservation.created_at,
        SourceObservation.source_id,
        literal("来源内容发生变化"),
    ).where(SourceObservation.owner_id == owner_id, SourceObservation.state == "pending")
    failures = select(
        CaptureSource.id,
        literal("failed"),
        CaptureSource.created_at,
        CaptureSource.id,
        literal("采集失败：") + func.coalesce(CaptureSource.error_code, literal("unknown")),
    ).where(CaptureSource.owner_id == owner_id, CaptureSource.capture_state == "failed")
    identities = select(
        IdentitySuggestion.id,
        literal("identity"),
        IdentitySuggestion.created_at,
        IdentitySuggestion.source_id,
        IdentitySuggestion.reason,
    ).where(IdentitySuggestion.owner_id == owner_id, IdentitySuggestion.state == "pending")
    combined = union_all(drafts, reviews, observations, failures, identities).subquery()
    stmt = select(combined)
    counts = dict(db.execute(select(combined.c.kind, func.count()).group_by(combined.c.kind)).all())
    if kind:
        stmt = stmt.where(combined.c.kind == kind)
    if query:
        stmt = stmt.where(combined.c.title.ilike(f"%{query}%"))
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (
                not isinstance(value, list)
                or len(value) != 3
                or not all(isinstance(part, str) for part in value)
            ):
                raise ValueError("cursor shape")
            stamp, last_kind, last_id = datetime.fromisoformat(value[0]), value[1], UUID(value[2])
            if last_kind not in {"draft", "review", "observation", "failed", "identity"}:
                raise ValueError("kind")
        except (ValueError, TypeError, IndexError, UnicodeError) as exc:
            raise AppError(422, "invalid_cursor", "分页游标无效") from exc
        stmt = stmt.where(
            or_(
                combined.c.created_at < stamp,
                and_(combined.c.created_at == stamp, combined.c.kind < last_kind),
                and_(
                    combined.c.created_at == stamp,
                    combined.c.kind == last_kind,
                    combined.c.id < last_id,
                ),
            )
        )
    rows = db.execute(
        stmt.order_by(
            combined.c.created_at.desc(), combined.c.kind.desc(), combined.c.id.desc()
        ).limit(limit + 1)
    ).all()
    items = [
        {
            "id": str(row.id),
            "kind": row.kind,
            "target_id": str(row.target_id),
            "title": row.title,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last["created_at"], last["kind"], last["id"]]).encode()
        ).decode()
    return {
        "items": items,
        "counts": counts,
        "total": sum(counts.values()),
        "next_cursor": next_cursor,
    }
