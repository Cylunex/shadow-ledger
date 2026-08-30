from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import AuditEvent, CaptureSource, LedgerRecordSource
from app.schemas import DirectoryIntakeEnvelope, StructuredIntake, jsonable
from app.services.records import create_record


class IntakeAdapter(Protocol):
    """Extension point for a source-specific mapper that returns the common intake contract."""

    name: str

    def parse(self, payload: dict[str, Any]) -> StructuredIntake: ...


class StructuredAdapter:
    name = "structured"

    def parse(self, payload: dict[str, Any]) -> StructuredIntake:
        return StructuredIntake.model_validate(payload)


def _source_type(channel: str, adapter: str) -> str:
    value = f"{channel}:{adapter}"
    if len(value) > 40:
        raise AppError(422, "invalid_intake_adapter", "抓单适配器名称过长")
    return value


def _linked_record_ids(db: Session, source_id) -> list[str]:
    return [
        str(value)
        for value in db.scalars(
            select(LedgerRecordSource.record_id)
            .where(LedgerRecordSource.source_id == source_id)
            .order_by(LedgerRecordSource.record_id)
        )
    ]


def _replay_source(db: Session, source: CaptureSource, raw: dict[str, Any]) -> dict[str, Any]:
    if source.raw_payload != {"intake": raw}:
        raise AppError(
            409,
            "intake_external_id_conflict",
            "相同来源标识不能用于不同抓单内容",
        )
    return {
        "source_id": str(source.id),
        "record_ids": _linked_record_ids(db, source.id),
        "state": source.capture_state,
        "replayed": True,
    }


def ingest_structured(
    db: Session,
    owner_id: str,
    channel: str,
    adapter: str,
    data: StructuredIntake,
    actor_id: str,
    actor_type: str = "service",
) -> dict[str, Any]:
    source_type = _source_type(channel, adapter)
    raw = jsonable(data.model_dump())
    existing = db.scalar(
        select(CaptureSource).where(
            CaptureSource.owner_id == owner_id,
            CaptureSource.source_type == source_type,
            CaptureSource.source_external_id == data.source_external_id,
        )
    )
    if existing:
        return _replay_source(db, existing, raw)

    source = CaptureSource(
        owner_id=owner_id,
        source_type=source_type,
        source_external_id=data.source_external_id,
        raw_payload={"intake": raw},
        parser=f"structured-intake:{adapter}"[:80],
        parser_version="1",
        capture_state="processing",
        captured_at=data.captured_at or datetime.now(UTC),
    )
    db.add(source)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        concurrent = db.scalar(
            select(CaptureSource).where(
                CaptureSource.owner_id == owner_id,
                CaptureSource.source_type == source_type,
                CaptureSource.source_external_id == data.source_external_id,
            )
        )
        if concurrent is None:
            raise
        return _replay_source(db, concurrent, raw)
    records = []
    try:
        for index, candidate in enumerate(data.records):
            # The schema rejects confirm=true. Keep the explicit copy so future schema changes
            # cannot silently widen this safety boundary.
            draft = candidate.model_copy(update={"confirm": False})
            record = create_record(
                db,
                owner_id,
                draft,
                f"intake:{source.id}:record:{index}",
                actor_id,
                actor_type=actor_type,
                commit=False,
            )
            db.add(
                LedgerRecordSource(
                    record_id=record.id,
                    source_id=source.id,
                    role="automated-intake",
                )
            )
            records.append(record)
        source.capture_state = "parsed"
        db.add(
            AuditEvent(
                owner_id=owner_id,
                actor_type=actor_type,
                actor_id=actor_id,
                action="intake.drafts_created",
                aggregate_type="capture_source",
                aggregate_id=source.id,
                details={"channel": channel, "adapter": adapter, "draft_count": len(records)},
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "source_id": str(source.id),
        "record_ids": [str(record.id) for record in records],
        "state": source.capture_state,
        "replayed": False,
    }


def read_owner_id(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("cannot read intake owner id file") from exc
    if not value or len(value) > 1000 or "\n" in value:
        raise RuntimeError("invalid intake owner id file")
    return value


def process_intake_directory(
    db: Session,
    directory: Path,
    owner_id: str,
    *,
    settle_seconds: float = 2.0,
    limit: int = 20,
) -> dict[str, int]:
    """Consume atomically-renamed JSON envelopes and move them to local result folders."""

    directory = directory.resolve()
    processed_dir = directory / "processed"
    failed_dir = directory / "failed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    counts = {"processed": 0, "failed": 0, "skipped": 0}
    current = datetime.now(UTC).timestamp()
    for path in sorted(directory.glob("*.json"))[:limit]:
        try:
            stat = path.stat()
            if stat.st_size > 1_000_000 or current - stat.st_mtime < settle_seconds:
                counts["skipped"] += 1
                continue
            raw = path.read_bytes()
            envelope = DirectoryIntakeEnvelope.model_validate_json(raw)
            ingest_structured(
                db,
                owner_id,
                "directory",
                envelope.adapter,
                envelope.payload,
                "directory-intake",
            )
            suffix = hashlib.sha256(raw).hexdigest()[:12]
            path.replace(processed_dir / f"{path.stem}-{suffix}.json")
            counts["processed"] += 1
        except (OSError, ValueError, ValidationError, AppError):
            db.rollback()
            try:
                suffix = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                path.replace(failed_dir / f"{path.stem}-{suffix}.json")
            except OSError:
                pass
            counts["failed"] += 1
    return counts
