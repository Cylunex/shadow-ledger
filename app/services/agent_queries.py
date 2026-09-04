"""Bounded, deterministic read models. Never accept SQL or a free-form formula."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.errors import AppError
from app.models import (
    AgentQueryRun,
    AuditEvent,
    BudgetTarget,
    ConsumptionEvent,
    ConsumptionLine,
    ForecastItem,
    ForecastRun,
    IdentitySuggestion,
    ItemIdentity,
    LedgerRecord,
    Merchant,
    MoneyEntry,
    RecurringCommitment,
)
from app.schemas import jsonable
from app.services.agent_canonical import aware, content_hash
from app.services.feedback import effective_feedback, feedback_map
from app.services.identities import family_ids

VERSION = "ledger-metrics-v1"
MAX_ROWS = 10000


def period(spec):
    try:
        start = datetime.strptime(spec.month, "%Y-%m").replace(tzinfo=ZoneInfo(spec.timezone))
        end = start + relativedelta(months=1)
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise AppError(422, "invalid_period", "月份或时区无效") from exc
    return start, end


def base_rows(db, owner_id, spec, *, entity=None):
    start, end = period(spec)
    stmt = (
        select(
            LedgerRecord.id,
            LedgerRecord.state,
            LedgerRecord.revision,
            LedgerRecord.occurred_at,
            LedgerRecord.updated_at,
            MoneyEntry.type,
            MoneyEntry.amount,
            MoneyEntry.currency,
            MoneyEntry.title,
            MoneyEntry.category_id,
            MoneyEntry.payment_method,
            ConsumptionEvent.scene,
            ConsumptionEvent.rating,
            ConsumptionEvent.would_repeat,
            ConsumptionEvent.merchant_id,
        )
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == owner_id,
            LedgerRecord.occurred_at >= start.astimezone(UTC),
            LedgerRecord.occurred_at < end.astimezone(UTC),
        )
    )
    if entity:
        kind, ids = entity
        stmt = (
            stmt.where(ConsumptionEvent.merchant_id.in_(ids))
            if kind == "merchant"
            else stmt.where(
                select(ConsumptionLine.id)
                .where(
                    ConsumptionLine.event_id == ConsumptionEvent.id,
                    ConsumptionLine.item_identity_id.in_(ids),
                )
                .exists()
            )
        )
    return list(
        db.execute(stmt.order_by(LedgerRecord.occurred_at, LedgerRecord.id).limit(MAX_ROWS + 1))
    )


def overview(db, ctx, spec):
    rows = base_rows(db, ctx.owner_id, spec)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    facts = [r for r in rows if r.state == "confirmed"]
    known = [r for r in facts if r.currency == spec.currency]
    unknown = sum(r.amount is None for r in facts)
    totals = {
        kind: sum((r.amount for r in known if r.type == kind), Decimal(0))
        for kind in ("expense", "income", "refund")
    }
    totals["net_spending"] = totals["expense"] - totals["refund"]
    status = "partial" if truncated or unknown else "exact"
    start, end = period(spec)
    metrics = [
        {
            "metric_id": key,
            "value": format(value, ".4f"),
            "currency": spec.currency,
            "status": status,
        }
        for key, value in totals.items()
    ]
    budgets = []
    if "ledger.budgets.read" in ctx.scopes and ctx.permissions.get("allow_budgets"):
        targets = list(
            db.scalars(
                select(BudgetTarget)
                .where(
                    BudgetTarget.owner_id == ctx.owner_id,
                    BudgetTarget.budget_month == start.date(),
                    BudgetTarget.currency == spec.currency,
                    BudgetTarget.active.is_(True),
                )
                .order_by(BudgetTarget.id)
                .limit(51)
            )
        )
        for target in targets[:50]:
            spent = sum(
                (
                    (r.amount if r.type == "expense" else -r.amount)
                    for r in known
                    if r.type in {"expense", "refund"}
                    and (target.category_id is None or r.category_id == target.category_id)
                ),
                Decimal(0),
            )
            budgets.append(
                {
                    "uri": f"shadow://ledger/budgets/{target.id}",
                    "target": format(target.monthly_amount, ".4f"),
                    "net_spending": format(spent, ".4f"),
                    "remaining": format(target.monthly_amount - spent, ".4f"),
                    "currency": spec.currency,
                    "status": status,
                    "revision": target.revision,
                }
            )
        budgets_truncated = len(targets) > 50
    else:
        budgets_truncated = False
    result = {
        "metrics": metrics,
        "period": {"from": start.isoformat(), "to": end.isoformat(), "timezone": spec.timezone},
        "source_count": len(known),
        "unknown_count": unknown,
        "excluded_count": len(rows) - len(known) - unknown,
        "truncated": truncated,
        "scan_limit": MAX_ROWS,
        "exchange_rate_applied": False,
        "data_watermark": max((aware(r.updated_at).isoformat() for r in rows), default=None),
        "provenance_refs": [f"shadow://ledger/records/{r.id}" for r in known[:50]],
        "provenance_truncated": len(known) > 50,
        "budgets": budgets,
        "budgets_truncated": budgets_truncated,
        "facts_text": f"{spec.month} {spec.timezone}：已知净支出 {format(totals['net_spending'], '.4f')} {spec.currency}；金额未知 {unknown} 条。"
        + ("查询已截断，不能作为完整合计。" if truncated else "不含草稿、撤销及其他币种。"),
    }
    return result


def search(db, ctx, spec):
    start, end = period(spec)
    stmt = (
        select(LedgerRecord, MoneyEntry, ConsumptionEvent)
        .select_from(LedgerRecord)
        .outerjoin(MoneyEntry, MoneyEntry.record_id == LedgerRecord.id)
        .outerjoin(ConsumptionEvent, ConsumptionEvent.record_id == LedgerRecord.id)
        .where(
            LedgerRecord.owner_id == ctx.owner_id,
            LedgerRecord.state == "confirmed",
            LedgerRecord.occurred_at >= start.astimezone(UTC),
            LedgerRecord.occurred_at < end.astimezone(UTC),
            (MoneyEntry.currency == spec.currency) | MoneyEntry.id.is_(None),
        )
    )
    for column, value in (
        (ConsumptionEvent.merchant_id, spec.merchant_id),
        (MoneyEntry.type, spec.money_type),
        (MoneyEntry.payment_method, spec.payment_method),
        (ConsumptionEvent.scene, spec.scene),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if spec.item_id:
        stmt = stmt.where(
            select(ConsumptionLine.id)
            .where(
                ConsumptionLine.event_id == ConsumptionEvent.id,
                ConsumptionLine.item_identity_id == spec.item_id,
            )
            .exists()
        )
    rows = list(
        db.execute(
            stmt.order_by(LedgerRecord.occurred_at.desc(), LedgerRecord.id)
            .offset(spec.offset)
            .limit(spec.limit + 1)
        )
    )
    items = []
    for record, entry, event in rows[: spec.limit]:
        item = {
            "uri": f"shadow://ledger/records/{record.id}",
            "revision": record.revision,
            "occurred_at": aware(record.occurred_at).isoformat(),
            "money_type": entry.type if entry else None,
            "amount": format(entry.amount, ".4f") if entry else None,
            "currency": entry.currency if entry else None,
            "payment_method": entry.payment_method if entry else None,
            "scene": event.scene if event else None,
            "merchant_uri": f"shadow://ledger/merchants/{event.merchant_id}"
            if event and event.merchant_id
            else None,
        }
        if ctx.disclosure == "local_private":
            item["title"] = entry.title if entry else None
        items.append(item)
    return {
        "items": items,
        "truncated": len(rows) > spec.limit,
        "next_offset": spec.offset + spec.limit if len(rows) > spec.limit else None,
        "facts_text": "以下为分页的已确认记录，不是完整流水合计。",
    }


def entity_insights(db, ctx, spec):
    model = Merchant if spec.kind == "merchant" else ItemIdentity
    entity = db.get(model, spec.entity_id)
    if entity is None or entity.owner_id != ctx.owner_id:
        raise AppError(404, "entity_not_found", "身份不存在")
    _, ids = family_ids(db, ctx.owner_id, spec.kind, entity.id)
    rows = base_rows(db, ctx.owner_id, spec, entity=(spec.kind, ids))
    truncated = len(rows) > MAX_ROWS
    facts = [
        r
        for r in rows[:MAX_ROWS]
        if r.state == "confirmed" and (r.type is None or r.type == "expense")
    ]
    deltas = [
        aware(b.occurred_at) - aware(a.occurred_at) for a, b in zip(facts, facts[1:], strict=False)
    ]
    intervals = [
        Decimal(d.days) + Decimal(d.seconds) / 86400 + Decimal(d.microseconds) / 86400000000
        for d in deltas
    ]
    metrics = [
        {
            "metric_id": "purchase_count",
            "value": str(len(facts)),
            "currency": None,
            "status": "partial" if truncated else "exact",
        }
    ]
    if intervals:
        metrics.append(
            {
                "metric_id": "mean_interval_days",
                "value": format(sum(intervals) / len(intervals), ".4f"),
                "currency": None,
                "status": "partial" if truncated else "exact",
            }
        )
    return {
        "entity_uri": f"shadow://ledger/{'merchants' if spec.kind == 'merchant' else 'items'}/{entity.id}",
        "canonical_name": entity.canonical_name,
        "metrics": metrics,
        "source_count": len(facts),
        "ratings": {str(n): sum(r.rating == n for r in facts) for n in range(1, 6)},
        "would_repeat_count": sum(r.would_repeat is True for r in facts),
        "unknown_count": sum(r.amount is None for r in facts),
        "truncated": truncated,
        "amount_basis": "not_computed_for_items_or_mixed_currencies",
        "facts_text": f"所选月份内观察到购买 {len(facts)} 次；不把整单金额当成商品单价。",
    }


def attention(db, ctx, spec):
    if spec.kind == "forecast":
        run = db.scalar(
            select(ForecastRun)
            .where(ForecastRun.owner_id == ctx.owner_id)
            .order_by(ForecastRun.created_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        if not run:
            return {"items": [], "truncated": False, "facts_text": "尚未生成预测。"}
        rows = list(
            db.scalars(
                select(ForecastItem)
                .where(ForecastItem.run_id == run.id, ForecastItem.expires_at > datetime.now(UTC))
                .order_by(ForecastItem.predicted_at, ForecastItem.id)
                .limit(501)
            )
        )
        decisions = feedback_map(db, ctx.owner_id, rows)
        visible = [r for r in rows[:500] if effective_feedback(r, decisions)["state"] == "active"]
        return jsonable(
            {
                "algorithm_version": run.algorithm_version,
                "as_of": run.as_of,
                "items": [
                    {
                        "uri": f"shadow://ledger/forecast-items/{r.id}",
                        "kind": r.kind,
                        "target_uri": r.target_uri,
                        "predicted_at": r.predicted_at,
                        "amount": r.expected_amount,
                        "currency": r.currency,
                        "status": "projected",
                        "reason_code": r.kind,
                        "confidence_kind": "heuristic_not_probability",
                    }
                    for r in visible[: spec.limit]
                ],
                "truncated": len(rows) > 500 or len(visible) > spec.limit,
                "facts_text": "预测是可过期建议，不是已发生消费。",
            }
        )
    if spec.kind == "recurring":
        rows = list(
            db.scalars(
                select(RecurringCommitment)
                .where(
                    RecurringCommitment.owner_id == ctx.owner_id,
                    RecurringCommitment.state == "active",
                )
                .order_by(RecurringCommitment.next_due_at, RecurringCommitment.id)
                .limit(spec.limit + 1)
            )
        )
        items = [
            {
                "uri": f"shadow://ledger/commitments/{r.id}",
                "due_at": aware(r.next_due_at).isoformat(),
                "amount": str(r.expected_amount) if r.expected_amount else None,
                "currency": r.currency,
                "status": "projected",
            }
            for r in rows[: spec.limit]
        ]
    elif spec.kind == "identity_merge":
        rows = list(
            db.scalars(
                select(IdentitySuggestion)
                .where(
                    IdentitySuggestion.owner_id == ctx.owner_id,
                    IdentitySuggestion.state == "pending",
                )
                .order_by(IdentitySuggestion.id)
                .limit(spec.limit + 1)
            )
        )
        items = [
            {
                "uri": f"shadow://ledger/identity-suggestions/{r.id}",
                "reason_code": "identity_review_required",
            }
            for r in rows[: spec.limit]
        ]
    else:
        stmt = (
            select(LedgerRecord).outerjoin(MoneyEntry).where(LedgerRecord.owner_id == ctx.owner_id)
        )
        stmt = (
            stmt.where(LedgerRecord.state == "draft")
            if spec.kind == "draft"
            else stmt.where(LedgerRecord.state == "confirmed", MoneyEntry.id.is_(None))
        )
        rows = list(
            db.scalars(
                stmt.order_by(LedgerRecord.created_at, LedgerRecord.id).limit(spec.limit + 1)
            )
        )
        items = [
            {
                "uri": f"shadow://ledger/records/{r.id}",
                "revision": r.revision,
                "state": r.state,
                "reason_code": spec.kind,
            }
            for r in rows[: spec.limit]
        ]
    return {
        "items": items,
        "truncated": len(rows) > spec.limit,
        "facts_text": "待处理摘要；本工具不会执行任何建议。",
    }


def persist_result(db, ctx, tool, spec, result, catalog_hash):
    query_id = uuid.uuid4()
    fingerprint = content_hash(
        {"aggregation_version": VERSION, "tool": tool, "spec": spec.model_dump(mode="json")}
    )
    result.setdefault("metrics", [])
    for budget in result.get("budgets", []):
        for key in ("target", "net_spending", "remaining"):
            result["metrics"].append(
                {
                    "metric_id": f"{budget['uri']}:{key}",
                    "value": budget[key],
                    "currency": budget["currency"],
                    "status": budget["status"],
                }
            )
    for item in result.get("items", []):
        if item.get("amount") is not None and item.get("status") == "projected":
            result["metrics"].append(
                {
                    "metric_id": f"{item['uri']}:amount",
                    "value": item["amount"],
                    "currency": item["currency"],
                    "status": "projected",
                }
            )
    result.setdefault("data_watermark", None)
    result["data_scope"] = "current_state_at_query_not_historical_ledger_snapshot"
    result["query_spec"] = spec.model_dump(mode="json")
    result = jsonable(
        {
            **result,
            "aggregation_version": VERSION,
            "query_fingerprint": fingerprint,
            "query_id": str(query_id),
            "query_ref": f"shadow://ledger/query-runs/{query_id}",
            "generated_at": datetime.now(UTC),
            "verification": {
                "verified": True,
                "verifier_version": VERSION,
                "scope": "deterministic_tool_result_not_host_language",
            },
        }
    )
    db.add(
        AgentQueryRun(
            id=query_id,
            owner_id=ctx.owner_id,
            agent_id=ctx.agent_id,
            catalog_hash=catalog_hash,
            query_fingerprint=fingerprint,
            result=result,
            result_hash=content_hash(result),
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    db.add(
        AuditEvent(
            owner_id=ctx.owner_id,
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="agent.query.completed",
            aggregate_type="agent_query",
            aggregate_id=query_id,
            details={
                "tool": tool,
                "catalog_hash": catalog_hash,
                "query_fingerprint": fingerprint,
                "truncated": bool(result.get("truncated")),
                "result_count": len(result.get("items", result.get("metrics", []))),
            },
        )
    )
    db.commit()
    return result


def explain(db, ctx, spec, catalog_hash):
    row = db.get(AgentQueryRun, spec.query_id)
    if row is None or row.owner_id != ctx.owner_id or row.agent_id != ctx.agent_id:
        raise AppError(404, "query_not_found", "查询凭证不存在")
    if aware(row.expires_at) <= datetime.now(UTC) or row.catalog_hash != catalog_hash:
        raise AppError(409, "query_context_expired", "查询或权限上下文已失效，请重新查询")
    if content_hash(row.result) != row.result_hash:
        raise AppError(409, "query_integrity_error", "查询结果完整性校验失败")
    metrics = {m["metric_id"]: m for m in row.result.get("metrics", [])}
    checks = []
    for claim in spec.claims:
        metric = metrics.get(claim.metric_id)
        try:
            matches = bool(
                metric
                and Decimal(metric["value"]) == Decimal(claim.value)
                and metric["currency"] == claim.currency
                and metric["status"] == claim.status
            )
        except Exception:
            matches = False
        checks.append({"metric_id": claim.metric_id, "verified": matches})
    return {
        "result": row.result,
        "result_hash": row.result_hash,
        "claim_checks": checks,
        "claims_verified": all(c["verified"] for c in checks) if checks else None,
        "notice": "只验证所提交结构化数字声明，不验证任意自然语言或第三方 Host 回答。",
    }
