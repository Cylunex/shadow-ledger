"""Retrospective comparison, not a claim of predictive accuracy or a historical ledger."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.models import ConsumptionEvent, ConsumptionLine, LedgerRecord, MoneyEntry
from app.services.forecast import build_input_snapshot, calculate


def backtest_repeat(db, owner_id, as_of, timezone, horizon_days):
    snapshot = build_input_snapshot(db, owner_id, as_of, timezone, horizon_days)
    predicted = [item for item in calculate(snapshot) if item["kind"] == "repeat_purchase"]
    start = datetime.combine(as_of, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    end = start + timedelta(days=horizon_days)
    actual = {}
    for item_id, record_id, occurred_at in db.execute(
        select(ConsumptionLine.item_identity_id, LedgerRecord.id, LedgerRecord.occurred_at)
        .join(ConsumptionEvent, ConsumptionEvent.id == ConsumptionLine.event_id)
        .join(LedgerRecord, LedgerRecord.id == ConsumptionEvent.record_id)
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == owner_id,
            LedgerRecord.state == "confirmed",
            ConsumptionLine.item_identity_id.is_not(None),
            LedgerRecord.occurred_at >= start,
            LedgerRecord.occurred_at < end,
            or_(MoneyEntry.id.is_(None), MoneyEntry.type == "expense"),
        )
        .order_by(LedgerRecord.occurred_at, LedgerRecord.id)
    ):
        actual.setdefault(str(item_id), (str(record_id), occurred_at))
    rows = []
    for prediction in predicted:
        observed = actual.get(prediction["target_uri"].rsplit("/", 1)[1])
        delta = None
        if observed:
            stamp = observed[1] if observed[1].tzinfo else observed[1].replace(tzinfo=UTC)
            delta = int(
                (stamp - datetime.fromisoformat(prediction["predicted_at"])).total_seconds()
                // 86400
            )
        rows.append(
            {
                "target_uri": prediction["target_uri"],
                "predicted_at": prediction["predicted_at"],
                "sample_count": prediction["evidence"]["sample_count"],
                "actual_record_id": observed[0] if observed else None,
                "actual_at": observed[1].isoformat() if observed else None,
                "days_actual_minus_predicted": delta,
                "result": "observed" if observed else "not_observed_in_window",
            }
        )
    return {
        "as_of": as_of.isoformat(),
        "horizon_days": horizon_days,
        "items": rows,
        "predicted_count": len(rows),
        "observed_count": sum(row["result"] == "observed" for row in rows),
        "notice": "使用当前已确认事实回看；不还原过去被修正或撤销的版本。预测只使用起始日前样本；未观察到不等于没买，不据此宣称准确率。",
    }
