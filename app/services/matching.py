"""Bounded, explainable matching. Suggestions never create a financial relationship."""

import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import CaptureSource, ConsumptionEvent, LedgerRecord, LedgerRecordSource, MoneyEntry
from app.schemas import RecordCreate, jsonable


def merchant_key(value):
    value = re.sub(r"[-－]订单(?:编号)?\d+.*$", "", value or "")
    return "".join(value.removesuffix(" 订单详情").casefold().split())


def order_refs(payload: dict | None) -> set[tuple[str, str]]:
    payload = payload or {}
    platform = payload.get("platform")
    if platform not in {"jd", "taobao", "meituan", "eleme", "alipay", "wechat"}:
        return set()
    result = set()
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in ("订单号", "商家订单号", "原订单号"):
            value = str(row.get(field) or "").strip()
            if platform == "meituan" and re.fullmatch(r"\d{8,}-[\da-f]+", value):
                value = value.split("-", 1)[0]
            if value and value not in {"/", "-"}:
                result.add((platform, value))
        if platform == "meituan":
            for value in re.findall(r"订单(?:编号)?(\d{8,})", str(row.get("订单标题", ""))):
                result.add((platform, value))
    return result


def refund_candidates(
    db: Session,
    owner_id: str,
    record: RecordCreate,
    *,
    payload=None,
    query: str | None = None,
    limit: int = 20,
) -> list[dict]:
    entry = record.money_entry
    if entry is None or entry.type != "refund":
        return []
    stmt = (
        select(MoneyEntry, LedgerRecord, ConsumptionEvent)
        .join(LedgerRecord, LedgerRecord.id == MoneyEntry.record_id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == owner_id,
            LedgerRecord.state.in_(("draft", "confirmed")),
            LedgerRecord.occurred_at >= record.occurred_at - timedelta(days=180),
            LedgerRecord.occurred_at <= record.occurred_at,
            MoneyEntry.type == "expense",
            MoneyEntry.currency == entry.currency,
            MoneyEntry.amount >= entry.amount,
        )
    )
    if query:
        pattern = f"%{query[:200]}%"
        stmt = stmt.where(
            or_(MoneyEntry.title.ilike(pattern), ConsumptionEvent.merchant_name_raw.ilike(pattern))
        )
    rows = list(
        db.execute(
            stmt.order_by(LedgerRecord.occurred_at.desc(), LedgerRecord.id.desc()).limit(200)
        )
    )
    ids = [row.id for _, row, _ in rows]
    refs = {record_id: set() for record_id in ids}
    if ids:
        for record_id, raw in db.execute(
            select(LedgerRecordSource.record_id, CaptureSource.raw_payload)
            .join(CaptureSource, CaptureSource.id == LedgerRecordSource.source_id)
            .where(LedgerRecordSource.record_id.in_(ids), CaptureSource.owner_id == owner_id)
        ):
            refs[record_id].update(order_refs(raw))
    amounts = (
        dict(
            db.execute(
                select(MoneyEntry.related_entry_id, func.sum(MoneyEntry.amount))
                .join(LedgerRecord, LedgerRecord.id == MoneyEntry.record_id)
                .where(
                    LedgerRecord.owner_id == owner_id,
                    LedgerRecord.state != "voided",
                    MoneyEntry.type == "refund",
                    MoneyEntry.related_entry_id.in_([money.id for money, _, _ in rows]),
                )
                .group_by(MoneyEntry.related_entry_id)
            ).all()
        )
        if rows
        else {}
    )
    incoming_refs = order_refs(payload)
    event = record.consumption
    result = []
    for money, target, consumption in rows:
        reasons = []
        score = 0
        if incoming_refs & refs[target.id]:
            reasons.append("same_platform_order")
            score += 100
        if event and consumption:
            if event.merchant_id and event.merchant_id == consumption.merchant_id:
                reasons.append("same_canonical_merchant")
                score += 50
            elif merchant_key(event.merchant_name_raw) and merchant_key(
                event.merchant_name_raw
            ) == merchant_key(consumption.merchant_name_raw):
                reasons.append("same_raw_merchant")
                score += 40
        if entry.title and merchant_key(entry.title) == merchant_key(money.title):
            reasons.append("same_title")
            score += 20
        if money.amount == entry.amount:
            reasons.append("same_amount")
            score += 5
        else:
            reasons.append("partial_refund_possible")
        refunded = Decimal(amounts.get(money.id) or 0)
        result.append(
            {
                "record_id": str(target.id),
                "entry_id": str(money.id),
                "revision": target.revision,
                "title": money.title,
                "merchant": consumption.merchant_name_raw if consumption else None,
                "amount": jsonable(money.amount),
                "currency": money.currency,
                "occurred_at": target.occurred_at.isoformat(),
                "reasons": reasons,
                "already_linked_refunds": jsonable(refunded),
                "exceeds_original": refunded + entry.amount > money.amount,
                "strength": "supported" if score >= 20 else "weak",
                "score": score,
            }
        )
    result.sort(key=lambda row: (row["score"], row["occurred_at"], row["record_id"]), reverse=True)
    return result[:limit]
