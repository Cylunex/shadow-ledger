from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ConsumptionEvent,
    ConsumptionLine,
    ForecastItem,
    ForecastRun,
    ItemIdentity,
    LedgerRecord,
    MoneyEntry,
    RecurringCommitment,
    UseCycle,
)
from app.schemas import jsonable

ALGORITHM_VERSION = "deterministic-v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _digest(value: Any) -> bytes:
    return hashlib.sha256(_canonical(value)).digest()


def _lower_median(values: list[Any]) -> Any:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def build_input_snapshot(
    db: Session, owner_id: str, as_of: date, timezone: str, horizon_days: int
) -> dict[str, Any]:
    cutoff = datetime.combine(as_of, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    commitments = [
        {
            "id": str(row.id),
            "kind": row.kind,
            "title": row.title,
            "item_identity_id": str(row.item_identity_id) if row.item_identity_id else None,
            "expected_amount": jsonable(row.expected_amount),
            "currency": row.currency,
            "next_due_at": row.next_due_at.isoformat(),
        }
        for row in db.scalars(
            select(RecurringCommitment)
            .where(
                RecurringCommitment.owner_id == owner_id,
                RecurringCommitment.state == "active",
            )
            .order_by(RecurringCommitment.id)
        )
    ]

    purchase_rows = db.execute(
        select(
            ConsumptionLine.item_identity_id,
            ItemIdentity.canonical_name,
            LedgerRecord.id,
            LedgerRecord.occurred_at,
            MoneyEntry.type,
            MoneyEntry.amount,
            MoneyEntry.currency,
        )
        .join(ConsumptionEvent, ConsumptionLine.event_id == ConsumptionEvent.id)
        .join(LedgerRecord, ConsumptionEvent.record_id == LedgerRecord.id)
        .join(ItemIdentity, ConsumptionLine.item_identity_id == ItemIdentity.id)
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at < cutoff,
            ConsumptionLine.item_identity_id.is_not(None),
        )
        .order_by(ConsumptionLine.item_identity_id, LedgerRecord.occurred_at, LedgerRecord.id)
    )
    purchases_by_record: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}
    for item_id, name, record_id, occurred_at, money_type, amount, currency in purchase_rows:
        key = (item_id, record_id)
        purchases_by_record[key] = {
            "item_identity_id": str(item_id),
            "item_name": name,
            "record_id": str(record_id),
            "occurred_at": occurred_at.isoformat(),
            "amount": jsonable(amount) if money_type == "expense" else None,
            "currency": currency if money_type == "expense" else None,
        }
    purchases = sorted(
        purchases_by_record.values(),
        key=lambda item: (item["item_identity_id"], item["occurred_at"], item["record_id"]),
    )

    cycles = []
    for row in db.scalars(
        select(UseCycle)
        .where(UseCycle.owner_id == owner_id, UseCycle.started_at < cutoff)
        .order_by(UseCycle.item_identity_id, UseCycle.started_at, UseCycle.id)
    ):
        ended_at = row.ended_at
        historical_state = row.state
        if ended_at is not None:
            normalized_end = ended_at.replace(tzinfo=UTC) if ended_at.tzinfo is None else ended_at
            if normalized_end >= cutoff:
                historical_state = "active"
                ended_at = None
        cycles.append(
            {
                "id": str(row.id),
                "item_identity_id": str(row.item_identity_id),
                "state": historical_state,
                "started_at": row.started_at.isoformat(),
                "expected_end_at": row.expected_end_at.isoformat() if row.expected_end_at else None,
                "ended_at": ended_at.isoformat() if ended_at else None,
            }
        )
    return {
        "as_of": as_of.isoformat(),
        "timezone": timezone,
        "horizon_days": horizon_days,
        "commitments": commitments,
        "purchases": purchases,
        "use_cycles": cycles,
    }


def calculate(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    as_of = date.fromisoformat(snapshot["as_of"])
    zone = ZoneInfo(snapshot["timezone"])
    start = datetime.combine(as_of, time.min, tzinfo=zone).astimezone(UTC)
    horizon_end = (
        datetime.combine(as_of, time.min, tzinfo=zone)
        + timedelta(days=int(snapshot["horizon_days"]))
    ).astimezone(UTC)
    expires_at = horizon_end + timedelta(days=1)
    output: list[dict[str, Any]] = []

    for row in snapshot["commitments"]:
        predicted_at = _parse_datetime(row["next_due_at"])
        if predicted_at > horizon_end:
            continue
        output.append(
            {
                "source_key": f"commitment:{row['id']}:{row['next_due_at']}",
                "kind": "commitment_due",
                "target_uri": f"shadow://ledger/recurring-commitments/{row['id']}",
                "predicted_at": predicted_at.isoformat(),
                "expected_amount": row["expected_amount"],
                "currency": row["currency"] if row["expected_amount"] else None,
                "confidence": "1.0000",
                "explanation": "来自用户明确设置的周期事项到期时间",
                "evidence": {"commitment_id": row["id"], "kind": row["kind"]},
                "expires_at": expires_at.isoformat(),
            }
        )

    purchases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["purchases"]:
        purchases[row["item_identity_id"]].append(row)
    for item_id, rows in purchases.items():
        if len(rows) < 3:
            continue
        occurred = [_parse_datetime(row["occurred_at"]) for row in rows]
        intervals = [
            int((right - left).total_seconds())
            for left, right in zip(occurred, occurred[1:], strict=False)
        ]
        positive = [value for value in intervals if value > 0]
        if len(positive) < 2:
            continue
        interval_seconds = _lower_median(positive)
        predicted_at = occurred[-1] + timedelta(seconds=interval_seconds)
        while predicted_at < start:
            predicted_at += timedelta(seconds=interval_seconds)
        if predicted_at > horizon_end:
            continue
        amounts_by_currency: dict[str, list[Decimal]] = defaultdict(list)
        for row in rows:
            if row["amount"] and row["currency"]:
                amounts_by_currency[row["currency"]].append(Decimal(row["amount"]))
        currency = None
        amount = None
        if amounts_by_currency:
            currency, values = sorted(
                amounts_by_currency.items(), key=lambda pair: (-len(pair[1]), pair[0])
            )[0]
            amount = format(_lower_median(values), ".4f")
        confidence = min(
            Decimal("0.9000"), Decimal("0.5000") + Decimal(len(rows) - 3) * Decimal("0.1000")
        )
        output.append(
            {
                "source_key": f"repeat-item:{item_id}:{rows[-1]['record_id']}:{interval_seconds}",
                "kind": "repeat_purchase",
                "target_uri": f"shadow://ledger/items/{item_id}",
                "predicted_at": predicted_at.isoformat(),
                "expected_amount": amount,
                "currency": currency if amount else None,
                "confidence": format(confidence, ".4f"),
                "explanation": f"根据 {len(rows)} 次已确认消费的典型间隔计算",
                "evidence": {
                    "sample_count": len(rows),
                    "interval_seconds": interval_seconds,
                    "record_ids": [row["record_id"] for row in rows],
                },
                "expires_at": expires_at.isoformat(),
            }
        )

    completed_durations: dict[str, list[int]] = defaultdict(list)
    active_cycles: list[dict[str, Any]] = []
    for row in snapshot["use_cycles"]:
        if row["state"] == "completed" and row["ended_at"]:
            duration = int(
                (
                    _parse_datetime(row["ended_at"]) - _parse_datetime(row["started_at"])
                ).total_seconds()
            )
            if duration > 0:
                completed_durations[row["item_identity_id"]].append(duration)
        elif row["state"] == "active":
            active_cycles.append(row)
    for row in active_cycles:
        evidence: dict[str, Any]
        if row["expected_end_at"]:
            predicted_at = _parse_datetime(row["expected_end_at"])
            confidence = Decimal("1.0000")
            explanation = "来自用户为本次使用周期设置的预计结束时间"
            evidence = {"use_cycle_id": row["id"], "source": "user_expected_end"}
        else:
            durations = completed_durations[row["item_identity_id"]]
            if len(durations) < 2:
                continue
            typical_duration = _lower_median(durations)
            predicted_at = _parse_datetime(row["started_at"]) + timedelta(seconds=typical_duration)
            confidence = min(
                Decimal("0.8500"),
                Decimal("0.5500") + Decimal(len(durations) - 2) * Decimal("0.1000"),
            )
            explanation = f"根据 {len(durations)} 个已完成使用周期的典型时长计算"
            evidence = {
                "use_cycle_id": row["id"],
                "sample_count": len(durations),
                "duration_seconds": typical_duration,
            }
        if predicted_at > horizon_end:
            continue
        output.append(
            {
                "source_key": f"use-cycle:{row['id']}:{predicted_at.isoformat()}",
                "kind": "use_cycle_end",
                "target_uri": f"shadow://ledger/use-cycles/{row['id']}",
                "predicted_at": predicted_at.isoformat(),
                "expected_amount": None,
                "currency": None,
                "confidence": format(confidence, ".4f"),
                "explanation": explanation,
                "evidence": evidence,
                "expires_at": expires_at.isoformat(),
            }
        )

    return sorted(output, key=lambda item: (item["predicted_at"], item["source_key"]))


def generate_forecast(
    db: Session, owner_id: str, as_of: date, timezone: str, horizon_days: int
) -> tuple[ForecastRun, list[ForecastItem], bool]:
    snapshot = build_input_snapshot(db, owner_id, as_of, timezone, horizon_days)
    input_hash = _digest(snapshot)
    existing = db.scalar(
        select(ForecastRun).where(
            ForecastRun.owner_id == owner_id,
            ForecastRun.algorithm_version == ALGORITHM_VERSION,
            ForecastRun.input_hash == input_hash,
        )
    )
    if existing:
        items = list(
            db.scalars(
                select(ForecastItem)
                .where(ForecastItem.run_id == existing.id)
                .order_by(ForecastItem.predicted_at, ForecastItem.source_key)
            )
        )
        return existing, items, True
    calculated = calculate(snapshot)
    run = ForecastRun(
        owner_id=owner_id,
        as_of=as_of,
        timezone=timezone,
        horizon_days=horizon_days,
        algorithm_version=ALGORITHM_VERSION,
        input_snapshot=snapshot,
        input_hash=input_hash,
        output_hash=_digest(calculated),
    )
    try:
        db.add(run)
        db.flush()
        items = [
            ForecastItem(
                run_id=run.id,
                source_key=item["source_key"],
                kind=item["kind"],
                target_uri=item["target_uri"],
                predicted_at=datetime.fromisoformat(item["predicted_at"]),
                expected_amount=Decimal(item["expected_amount"])
                if item["expected_amount"]
                else None,
                currency=item["currency"],
                confidence=Decimal(item["confidence"]),
                explanation=item["explanation"],
                evidence=item["evidence"],
                expires_at=datetime.fromisoformat(item["expires_at"]),
            )
            for item in calculated
        ]
        db.add_all(items)
        db.commit()
        return run, items, False
    except IntegrityError:
        db.rollback()
        concurrent = db.scalar(
            select(ForecastRun).where(
                ForecastRun.owner_id == owner_id,
                ForecastRun.algorithm_version == ALGORITHM_VERSION,
                ForecastRun.input_hash == input_hash,
            )
        )
        if concurrent is None:
            raise
        items = list(
            db.scalars(
                select(ForecastItem)
                .where(ForecastItem.run_id == concurrent.id)
                .order_by(ForecastItem.predicted_at, ForecastItem.source_key)
            )
        )
        return concurrent, items, True


def verify_run(run: ForecastRun) -> bool:
    return _digest(calculate(run.input_snapshot)) == run.output_hash


def serialize_item(item: ForecastItem) -> dict[str, Any]:
    return jsonable(
        {
            "id": item.id,
            "source_key": item.source_key,
            "kind": item.kind,
            "target_uri": item.target_uri,
            "predicted_at": item.predicted_at,
            "expected_amount": item.expected_amount,
            "currency": item.currency,
            "confidence": item.confidence,
            "explanation": item.explanation,
            "evidence": item.evidence,
            "expires_at": item.expires_at,
            "state": item.state,
            "revision": item.revision,
        }
    )


def serialize_run(
    run: ForecastRun, items: list[ForecastItem], replayed: bool = False
) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "as_of": run.as_of.isoformat(),
        "timezone": run.timezone,
        "horizon_days": run.horizon_days,
        "algorithm_version": run.algorithm_version,
        "input_hash": run.input_hash.hex(),
        "output_hash": run.output_hash.hex(),
        "created_at": run.created_at.isoformat(),
        "replayed": replayed,
        "items": [serialize_item(item) for item in items],
    }
