"""User decisions are separate from immutable forecast computation output."""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import AuditEvent, ForecastItem, ForecastRun, SuggestionFeedback


def episode_key(item: ForecastItem) -> str:
    if item.kind == "repeat_purchase":
        # Rolling a projected date forward is NOT a new actual purchase.
        anchor = (item.evidence.get("record_ids") or [item.source_key])[-1]
    elif item.kind == "use_cycle_end":
        anchor = item.evidence.get("use_cycle_id", item.target_uri)
    else:
        anchor = item.source_key
    return hashlib.sha256(f"{item.kind}|{item.target_uri}|{anchor}".encode()).hexdigest()


def feedback_map(db: Session, owner_id: str, items: list[ForecastItem]):
    keys = [episode_key(item) for item in items]
    if not keys:
        return {}
    return {
        row.episode_key: row
        for row in db.scalars(
            select(SuggestionFeedback).where(
                SuggestionFeedback.owner_id == owner_id, SuggestionFeedback.episode_key.in_(keys)
            )
        )
    }


def effective_feedback(item: ForecastItem, feedback, at: datetime | None = None) -> dict:
    at = at or datetime.now(UTC)
    row = feedback.get(episode_key(item))
    state = row.state if row else item.state
    until = row.snoozed_until if row else None
    if until and until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if state == "snoozed" and until <= at:
        state = "active"
    return {
        "state": state,
        "feedback_revision": row.revision if row else 0,
        "snoozed_until": until.isoformat() if until else None,
        "episode_key": episode_key(item),
    }


def decorate_run(db, run: ForecastRun, items: list[ForecastItem], result: dict) -> dict:
    mapping = feedback_map(db, run.owner_id, items)
    for item, serialized in zip(items, result["items"], strict=True):
        serialized.update(effective_feedback(item, mapping))
        serialized["confidence_kind"] = (
            "explicit_schedule" if item.kind == "commitment_due" else "heuristic_not_probability"
        )
    return result


def save_feedback(
    db: Session,
    owner_id: str,
    item_id,
    item_revision: int,
    expected_revision: int | None,
    state: str,
    snoozed_until=None,
):
    item = db.scalar(
        select(ForecastItem)
        .join(ForecastRun)
        .where(ForecastItem.id == item_id, ForecastRun.owner_id == owner_id)
        .with_for_update(of=ForecastItem)
    )
    if item is None:
        raise AppError(404, "forecast_item_not_found", "预测项不存在")
    if item.revision != item_revision:
        raise AppError(409, "revision_conflict", "预测项已更新，请刷新")
    key = episode_key(item)
    row = db.scalar(
        select(SuggestionFeedback)
        .where(SuggestionFeedback.owner_id == owner_id, SuggestionFeedback.episode_key == key)
        .with_for_update()
    )
    if expected_revision is None and row:
        # Legacy dismiss may replay the same decision, but cannot overwrite newer feedback.
        if state == row.state == "dismissed":
            return item, row
        raise AppError(409, "revision_conflict", "建议已有新决定，请刷新")
    if (row.revision if row else 0) != (expected_revision or 0):
        raise AppError(409, "revision_conflict", "建议已被其他操作处理，请刷新")
    if state == "snoozed":
        if (
            snoozed_until is None
            or snoozed_until.tzinfo is None
            or snoozed_until <= datetime.now(UTC)
        ):
            raise AppError(422, "invalid_snooze", "稍后提醒时间必须含时区且在未来")
    if row is None:
        row = SuggestionFeedback(owner_id=owner_id, episode_key=key, state=state, revision=1)
        db.add(row)
    else:
        row.revision += 1
    row.state = state
    row.snoozed_until = snoozed_until if state == "snoozed" else None
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=owner_id,
            action="forecast.feedback",
            aggregate_type="forecast_item",
            aggregate_id=item.id,
            details={"state": state},
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(409, "revision_conflict", "建议已被其他操作处理，请刷新") from exc
    return item, row
