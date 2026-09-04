"""Source observations preserve the original evidence and never autonomously change facts."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.models import (
    AuditEvent,
    CaptureSource,
    LedgerRecord,
    LedgerRecordSource,
    SourceObservation,
)
from app.schemas import RecordCreate, RecordPatch, jsonable
from app.services.records import patch_record, record_query, request_hash


def field_evidence(payload, candidate=None):
    result = []
    rows = payload.get("rows", [])
    for index, row in enumerate(rows[:1000] if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        for column, value in row.items():
            if column.startswith("_"):
                continue
            result.append(
                {
                    "field": f"rows.{index}.{column}",
                    "raw_value": str(value),
                    "locator": {
                        "format": payload.get("format", "markdown"),
                        "line": row.get("_source_line"),
                        "column": column,
                    },
                }
            )
    if not result:
        result.append(
            {
                "field": "payload",
                "locator": {"format": "json", "pointer": "/"},
                "note": "结构化来源；无图片坐标",
            }
        )
    return result


def append_observation(
    db,
    source,
    external_revision,
    payload,
    candidate=None,
    *,
    parser=None,
    parser_version=None,
    baseline=False,
):
    list(
        db.scalars(
            select(LedgerRecord)
            .join(LedgerRecordSource, LedgerRecordSource.record_id == LedgerRecord.id)
            .where(
                LedgerRecordSource.source_id == source.id, LedgerRecord.owner_id == source.owner_id
            )
            .order_by(LedgerRecord.id)
            .with_for_update(of=LedgerRecord)
        )
    )
    source = db.scalar(
        select(CaptureSource)
        .where(CaptureSource.id == source.id, CaptureSource.owner_id == source.owner_id)
        .with_for_update()
    )
    normalized = jsonable(candidate.model_dump()) if candidate else None
    digest = request_hash(payload)
    parser = parser or source.parser or "structured"
    parser_version = parser_version or source.parser_version or "1"
    existing = db.scalar(
        select(SourceObservation).where(
            SourceObservation.source_id == source.id,
            SourceObservation.external_revision == external_revision,
        )
    )
    if existing:
        if (
            existing.raw_hash != digest
            or existing.candidate != normalized
            or existing.parser != parser
            or existing.parser_version != parser_version
        ):
            raise AppError(
                409, "observation_version_conflict", "同一观察版本不能使用不同内容或解析结果"
            )
        return existing, True
    observation = SourceObservation(
        owner_id=source.owner_id,
        source_id=source.id,
        external_revision=external_revision,
        raw_hash=digest,
        payload=jsonable(payload),
        candidate=normalized,
        field_evidence=field_evidence(payload, candidate),
        parser=parser,
        parser_version=parser_version,
        state="baseline" if baseline else "pending",
    )
    db.add(observation)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(
            409, "observation_version_conflict", "来源已更新，请重试并检查观察版本"
        ) from exc
    return observation, False


def ensure_baseline(db, source):
    existing = db.scalar(
        select(SourceObservation).where(
            SourceObservation.source_id == source.id,
            SourceObservation.external_revision == "initial",
        )
    )
    if existing:
        return existing
    return append_observation(
        db, source, "initial", source.raw_payload or {"text": source.raw_text}, baseline=True
    )[0]


def comparable_payload(payload):
    """Location metadata added in v2 must not make unchanged legacy rows look changed."""
    if isinstance(payload, dict):
        return {
            key: comparable_payload(value)
            for key, value in payload.items()
            if key not in {"_source_line", "format"}
        }
    if isinstance(payload, list):
        return [comparable_payload(value) for value in payload]
    return payload


def serialize_observation(row):
    return jsonable(
        {
            "id": row.id,
            "source_id": row.source_id,
            "external_revision": row.external_revision,
            "raw_hash": row.raw_hash.hex(),
            "payload": row.payload,
            "candidate": row.candidate,
            "field_evidence": row.field_evidence,
            "parser": row.parser,
            "parser_version": row.parser_version,
            "schema_version": row.schema_version,
            "state": row.state,
            "resolution": row.resolution,
            "revision": row.revision,
            "created_at": row.created_at,
        }
    )


def decide_observation(db, owner_id, observation_id, data):
    initial = db.scalar(
        select(SourceObservation).where(
            SourceObservation.id == observation_id, SourceObservation.owner_id == owner_id
        )
    )
    if initial is None:
        raise AppError(404, "observation_not_found", "观察不存在")
    # Same lock order as record confirmation/review; never lock nullable eager joins.
    if data.action == "apply":
        record = db.scalar(
            record_query()
            .where(LedgerRecord.id == data.record_id, LedgerRecord.owner_id == owner_id)
            .with_for_update(of=LedgerRecord)
        )
        link = db.scalar(
            select(LedgerRecordSource).where(
                LedgerRecordSource.source_id == initial.source_id,
                LedgerRecordSource.record_id == data.record_id,
            )
        )
        if record is None or link is None:
            raise AppError(404, "record_not_found", "请选择此来源关联的本人记录")
        if record.revision != data.record_revision:
            raise AppError(409, "revision_conflict", "记录已变化，请重新复核")
    row = db.scalar(
        select(SourceObservation)
        .where(SourceObservation.id == initial.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.revision != data.revision or row.state != "pending":
        raise AppError(409, "revision_conflict", "观察已处理或发生变化")
    if data.action == "apply":
        if row.candidate is None:
            raise AppError(422, "no_candidate", "此观察没有候选记录，请手工修正后保留事实")
        candidate = RecordCreate.model_validate(row.candidate)
        if (record.money_entry is not None) != (candidate.money_entry is not None) or (
            (record.consumption is not None) != (candidate.consumption is not None)
        ):
            raise AppError(
                422, "record_kind_immutable", "候选与原记录结构不同，请保留原事实并另建草稿"
            )
        patch_record(
            db,
            owner_id,
            record.id,
            data.record_revision,
            RecordPatch(
                occurred_at=candidate.occurred_at,
                timezone=candidate.timezone,
                note=candidate.note,
                money_entry=candidate.money_entry,
                consumption=candidate.consumption,
                correction_reason=data.reason,
            ),
            owner_id,
            commit=False,
        )
        row.state = "applied"
    else:
        row.state = "kept"
    row.resolution = {
        "action": data.action,
        "reason": data.reason,
        "record_id": str(data.record_id) if data.record_id else None,
    }
    row.revision += 1
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=owner_id,
            action="source.observation.decided",
            aggregate_type="source_observation",
            aggregate_id=row.id,
            details={"action": data.action},
        )
    )
    db.commit()
    return serialize_observation(row)
