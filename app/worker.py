from __future__ import annotations

import csv
import io
import json
import logging
import os
import signal
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as database
from app.config import get_settings
from app.errors import AppError
from app.integrations import AssetClient, CaptureParserClient
from app.models import (
    AssetBinding,
    BackgroundJob,
    BudgetTarget,
    CaptureSource,
    ExternalReference,
    ForecastItem,
    ForecastRun,
    ItemIdentity,
    LedgerRecord,
    LedgerRecordSource,
    Merchant,
    MoneyCategory,
    OutboxEvent,
    RecurringCommitment,
    Reminder,
    SourceObservation,
    SpendingIntent,
    SuggestionFeedback,
    UseCycle,
)
from app.schemas import RecordCreate, jsonable
from app.services.forecast import ALGORITHM_VERSION, generate_forecast
from app.services.intake import process_intake_directory, read_owner_id
from app.services.records import create_record, record_query, serialize_record

log = logging.getLogger("ledger.worker")
stopping = False


def stop(*_: object) -> None:
    global stopping
    stopping = True


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def export_row(row: object) -> dict[str, object]:
    from sqlalchemy import inspect

    return {
        column.key: jsonable(getattr(row, column.key))
        for column in inspect(type(row)).mapper.column_attrs
    }


def create_due_reminders(db: Session) -> int:
    current = datetime.now(UTC)
    rows = db.scalars(
        select(RecurringCommitment)
        .where(
            RecurringCommitment.state == "active",
            RecurringCommitment.next_due_at <= current + timedelta(days=365),
        )
        .order_by(RecurringCommitment.id)
        .with_for_update(skip_locked=True)
    )
    created = 0
    for row in rows:
        due_at = as_utc(row.next_due_at)
        reminder_at = due_at - timedelta(seconds=row.remind_before_seconds)
        if reminder_at > current:
            continue
        key = f"commitment:{row.id}:occurrence:{due_at.isoformat()}"
        exists = db.scalar(
            select(Reminder.id).where(
                Reminder.owner_id == row.owner_id, Reminder.reminder_key == key
            )
        )
        if not exists:
            db.add(
                Reminder(
                    owner_id=row.owner_id,
                    reminder_key=key,
                    source_type="commitment",
                    source_id=row.id,
                    due_at=due_at,
                    state="pending",
                    payload={"title": row.title},
                )
            )
            created += 1
        try:
            local_due = due_at.astimezone(ZoneInfo(row.timezone))
            rule = rrulestr(row.recurrence_rule, dtstart=local_due)
            next_due = rule.after(local_due, inc=False)
            if next_due:
                row.next_due_at = next_due.astimezone(UTC)
            else:
                row.state = "ended"
            row.revision += 1
        except (ValueError, TypeError):
            log.warning("invalid recurrence rule", extra={"aggregate_id": str(row.id)})
    db.commit()
    return created


def generate_daily_forecasts(db: Session) -> int:
    """Create at most one deterministic suggestion run per owner and local day.

    Forecasts are derived suggestions only. This path never creates LedgerRecord,
    MoneyEntry or ConsumptionEvent facts and never confirms a draft.
    """
    settings = get_settings()
    if not settings.auto_forecast_enabled:
        return 0
    today = datetime.now(ZoneInfo(settings.default_timezone)).date()
    owners = set(db.scalars(select(LedgerRecord.owner_id).distinct()))
    owners.update(db.scalars(select(RecurringCommitment.owner_id).distinct()))
    owners.update(db.scalars(select(UseCycle.owner_id).distinct()))
    created = 0
    for owner_id in sorted(owners):
        exists = db.scalar(
            select(ForecastRun.id).where(
                ForecastRun.owner_id == owner_id,
                ForecastRun.as_of == today,
                ForecastRun.timezone == settings.default_timezone,
                ForecastRun.horizon_days == settings.auto_forecast_horizon_days,
                ForecastRun.algorithm_version == ALGORITHM_VERSION,
            )
        )
        if exists is not None:
            continue
        _, _, replayed = generate_forecast(
            db,
            owner_id,
            today,
            settings.default_timezone,
            settings.auto_forecast_horizon_days,
        )
        created += int(not replayed)
    return created


def process_job(db: Session, job: BackgroundJob) -> None:
    if job.job_type == "capture.parse":
        source = db.get(CaptureSource, uuid.UUID(job.payload["source_id"]))
        if source is None:
            raise ValueError("source_not_found")
        if source.asset_id is None:
            raise ValueError("asset_missing")
        source.capture_state = "processing"
        db.commit()
        result = CaptureParserClient(get_settings()).parse(source.id, source.asset_id)
        from app.services.intake_reviews import register_intake_reviews
        from app.services.records import request_hash
        from app.services.sources import append_observation, ensure_baseline

        raw_candidates = result.get("candidates", [])
        if not isinstance(raw_candidates, list) or len(raw_candidates) > 100:
            raise ValueError("invalid_candidate_count")
        candidates = [
            RecordCreate.model_validate({**raw, "confirm": False}) for raw in raw_candidates
        ]
        parsed_payload = {
            "fields": result.get("fields", {}),
            "candidates": [jsonable(candidate.model_dump()) for candidate in candidates],
        }
        prior_links = list(
            db.scalars(select(LedgerRecordSource).where(LedgerRecordSource.source_id == source.id))
        )
        if prior_links:
            ensure_baseline(db, source)
            append_observation(
                db,
                source,
                request_hash(parsed_payload).hex(),
                parsed_payload,
                candidates[0] if len(candidates) == 1 and len(prior_links) == 1 else None,
                parser=str(result.get("parser", "external"))[:80],
                parser_version=str(result.get("parser_version", "1"))[:40],
            )
        else:
            source.parser = str(result.get("parser", "external"))[:80]
            source.parser_version = str(result.get("parser_version", "1"))[:40]
            source.raw_payload = parsed_payload
            records = []
            for index, candidate in enumerate(candidates):
                record = create_record(
                    db,
                    source.owner_id,
                    candidate,
                    f"capture:{source.id}:candidate:{index}",
                    "capture-parser",
                    actor_type="service",
                    commit=False,
                )
                records.append(record)
                db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="capture"))
            ensure_baseline(db, source)
            if records:
                register_intake_reviews(db, source.owner_id, source, records, candidates)
        source.capture_state = "parsed"
        source.error_code = None
    elif job.job_type == "export":
        owner_id = str(job.payload["owner_id"])
        export_format = str(job.payload["format"])
        records = list(
            db.scalars(
                record_query()
                .where(LedgerRecord.owner_id == owner_id)
                .order_by(LedgerRecord.occurred_at, LedgerRecord.id)
            )
        )
        if export_format == "json":
            collections = {
                "categories": MoneyCategory,
                "capture_sources": CaptureSource,
                "asset_bindings": AssetBinding,
                "external_references": ExternalReference,
                "merchants": Merchant,
                "items": ItemIdentity,
                "intents": SpendingIntent,
                "recurring_commitments": RecurringCommitment,
                "budget_targets": BudgetTarget,
                "reminders": Reminder,
                "use_cycles": UseCycle,
                "forecast_runs": ForecastRun,
                "source_observations": SourceObservation,
                "suggestion_feedback": SuggestionFeedback,
            }
            forecast_run_ids = list(
                db.scalars(select(ForecastRun.id).where(ForecastRun.owner_id == owner_id))
            )
            content = json.dumps(
                {
                    "version": 1,
                    "records": [serialize_record(db, row) for row in records],
                    **{
                        name: [
                            export_row(row)
                            for row in db.scalars(select(model).where(model.owner_id == owner_id))
                        ]
                        for name, model in collections.items()
                    },
                    "forecast_items": [
                        export_row(row)
                        for row in db.scalars(
                            select(ForecastItem).where(ForecastItem.run_id.in_(forecast_run_ids))
                        )
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            mime_type = "application/json"
        elif export_format == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(
                [
                    "record_id",
                    "state",
                    "occurred_at",
                    "money_type",
                    "amount",
                    "currency",
                    "title",
                    "scene",
                    "merchant_name_raw",
                    "payment_method",
                ]
            )
            for row in records:
                writer.writerow(
                    [
                        row.id,
                        row.state,
                        row.occurred_at.isoformat(),
                        row.money_entry.type if row.money_entry else "",
                        jsonable(row.money_entry.amount) if row.money_entry else "",
                        row.money_entry.currency if row.money_entry else "",
                        row.money_entry.title if row.money_entry else "",
                        row.consumption.scene if row.consumption else "",
                        row.consumption.merchant_name_raw if row.consumption else "",
                        row.money_entry.payment_method if row.money_entry else "",
                    ]
                )
            content = output.getvalue().encode("utf-8-sig")
            mime_type = "text/csv"
        else:
            raise ValueError("invalid_export_format")
        asset_id = AssetClient(get_settings()).store_export(
            owner_id,
            f"shadow-ledger-export.{export_format}",
            mime_type,
            content,
            f"ledger-export:{job.id}",
        )
        job.payload = {**job.payload, "asset_id": str(asset_id)}
    else:
        raise ValueError("unknown_job_type")


def claim_and_run_job(db: Session, worker_id: str) -> bool:
    current = datetime.now(UTC)
    job = db.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.state.in_(["pending", "failed"]),
            BackgroundJob.available_at <= current,
            BackgroundJob.attempts < 8,
        )
        .order_by(BackgroundJob.available_at, BackgroundJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return False
    job.state = "running"
    job.locked_at = current
    job.locked_by = worker_id
    job.attempts += 1
    db.commit()
    try:
        process_job(db, job)
        job.state = "succeeded"
        job.last_error_code = None
    except Exception as exc:
        job.state = "failed"
        job.last_error_code = getattr(exc, "code", None) or str(exc)[:80]
        job.available_at = current + timedelta(seconds=min(3600, 2**job.attempts))
        if job.job_type == "capture.parse" and job.payload.get("source_id"):
            source = db.get(CaptureSource, uuid.UUID(job.payload["source_id"]))
            if source:
                source.capture_state = "failed"
                source.error_code = job.last_error_code
    job.updated_at = datetime.now(UTC)
    db.commit()
    return True


def deliver_outbox(db: Session) -> bool:
    settings = get_settings()
    current = datetime.now(UTC)
    event = db.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.delivered_at.is_(None), OutboxEvent.available_at <= current)
        .order_by(OutboxEvent.available_at, OutboxEvent.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if event is None:
        return False
    event.attempts += 1
    try:
        if event.event_type in {"ledger.record.confirmed", "ledger.record.voided"}:
            event.delivered_at = current
            event.last_error_code = None
        elif event.event_type == "ledger.asset.reference.create":
            reference_id = AssetClient(settings).create_reference(event.payload)
            binding = db.scalar(
                select(AssetBinding).where(
                    AssetBinding.source_type == "capture_source",
                    AssetBinding.source_id == event.aggregate_id,
                    AssetBinding.released_at.is_(None),
                )
            )
            if binding:
                binding.asset_reference_id = reference_id
            event.delivered_at = current
            event.last_error_code = None
        else:
            raise AppError(503, "adapter_not_configured", "集成适配器未配置")
    except Exception as exc:
        event.last_error_code = getattr(exc, "code", None) or "delivery_failed"
        event.available_at = current + timedelta(seconds=min(3600, 2**event.attempts))
    db.commit()
    return True


def run_once(worker_id: str | None = None) -> int:
    if database.SessionLocal is None:
        database.init_database()
    assert database.SessionLocal is not None
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    completed = 0
    with database.SessionLocal() as db:
        settings = get_settings()
        if settings.intake_directory and settings.intake_owner_id_file:
            result = process_intake_directory(
                db,
                settings.intake_directory,
                read_owner_id(settings.intake_owner_id_file),
            )
            completed += result["processed"] + result["failed"]
        completed += generate_daily_forecasts(db)
        completed += create_due_reminders(db)
        completed += int(claim_and_run_job(db, worker_id))
        completed += int(deliver_outbox(db))
    return completed


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    database.init_database()
    settings = get_settings()
    while not stopping:
        worked = run_once()
        if not worked:
            time.sleep(settings.worker_poll_seconds)
