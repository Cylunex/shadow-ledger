"""One allowlisted gateway for browser, machine and MCP adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select

from app.agent_schemas import (
    AttachSpec,
    AttentionSpec,
    DraftSpec,
    EntitySpec,
    ExplainSpec,
    OverviewSpec,
    ParseSpec,
    ReviseSpec,
    SearchSpec,
)
from app.errors import AppError
from app.models import (
    AgentCatalogSnapshot,
    AuditEvent,
    CaptureSource,
    LedgerAgentGrant,
    LedgerRecordSource,
    MoneyCategory,
)
from app.schemas import MoneyEntryInput, RecordCreate, RecordPatch
from app.services import agent_queries as queries
from app.services.agent_canonical import POLICY_DIGEST, capability_hash, content_hash
from app.services.agent_effects import locked_record
from app.services.records import (
    create_record,
    idempotency_lookup,
    idempotency_save,
    lock_command,
    patch_record,
)

TOOLS = {
    "ledger_overview": (
        OverviewSpec,
        "ledger.summary.read",
        "allow_summary",
        {"research"},
        "查询单月、单币种确定性摘要与数字证据",
    ),
    "ledger_search": (
        SearchSpec,
        "ledger.records.read",
        "allow_records",
        {"research"},
        "分页检索确认记录，不返回原文或任意 SQL",
    ),
    "ledger_entity_insights": (
        EntitySpec,
        "ledger.records.read",
        "allow_records",
        {"research"},
        "查询规范身份的次数、间隔和评分分布",
    ),
    "ledger_attention": (
        AttentionSpec,
        "ledger.records.read",
        "allow_records",
        {"research", "steward"},
        "读取建议和待处理摘要，绝不执行",
    ),
    "ledger_explain": (
        ExplainSpec,
        "",
        "",
        {"research", "steward"},
        "查看本授权上下文查询凭证并验证结构化数字声明",
    ),
    "ledger_parse_capture": (
        ParseSpec,
        "ledger.records.draft",
        "allow_drafts",
        {"capture"},
        "确定性解析不可信文本，不写库、不访问 URL",
    ),
    "ledger_create_draft": (
        DraftSpec,
        "ledger.records.draft",
        "allow_drafts",
        {"capture"},
        "幂等创建 money-only 草稿，必须人工确认",
    ),
    "ledger_revise_draft": (
        ReviseSpec,
        "ledger.records.draft",
        "allow_drafts",
        {"capture"},
        "只修改本 Agent 的 money-only 草稿，检查版本",
    ),
    "ledger_attach_source": (
        AttachSpec,
        "ledger.records.draft",
        "allow_drafts",
        {"capture"},
        "绑定本 Agent 已关联过的已有来源，不上传或读取文件",
    ),
}


def skill_instructions(skill):
    if skill not in {"research", "capture", "steward"}:
        raise AppError(422, "invalid_agent_skill", "任务 Skill 无效")
    app_root = Path(__file__).resolve().parents[1]
    root = (
        app_root / "release" / "agent"
        if (app_root / "release" / "agent").is_dir()
        else app_root.parent / "agent"
    )
    return (root / "skills" / f"ledger-{skill}" / "SKILL.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class AgentContext:
    owner_id: str
    agent_id: str
    scopes: frozenset[str]
    permissions: dict
    authorization_hash: str
    disclosure: str = "remote_minimal"


def machine_context(db, identity):
    grant = db.scalar(
        select(LedgerAgentGrant).where(
            LedgerAgentGrant.agent_id == identity.agent_id, LedgerAgentGrant.active.is_(True)
        )
    )
    if grant is None:
        raise AppError(404, "ledger_grant_not_found", "Ledger 资源授权不存在")
    return AgentContext(
        grant.owner_id,
        identity.agent_id,
        frozenset(identity.scopes),
        {
            name: bool(getattr(grant, name))
            for name in ("allow_summary", "allow_records", "allow_budgets", "allow_drafts")
        },
        capability_hash(grant),
    )


def local_context(
    owner_id, *, allow_drafts=False, disclosure="remote_minimal", agent_id="ledger-local"
):
    scopes = {"ledger.summary.read", "ledger.records.read", "ledger.budgets.read"}
    if allow_drafts:
        scopes.add("ledger.records.draft")
    permissions = {
        "allow_summary": True,
        "allow_records": True,
        "allow_budgets": True,
        "allow_drafts": allow_drafts,
    }
    return AgentContext(
        owner_id,
        agent_id,
        frozenset(scopes),
        permissions,
        content_hash({"owner": owner_id, "permissions": permissions}),
        disclosure,
    )


def compile_catalog(db, ctx, skill, *, persist=True):
    if skill not in {"research", "capture", "steward"} or ctx.disclosure not in {
        "remote_minimal",
        "local_private",
    }:
        raise AppError(422, "invalid_agent_profile", "任务或披露模式无效")
    tools = []
    for name, (schema, scope, permission, skills, description) in sorted(TOOLS.items()):
        if skill not in skills:
            continue
        if scope and (scope not in ctx.scopes or not ctx.permissions.get(permission)):
            continue
        if name == "ledger_explain" and not any(
            ctx.permissions.get(p) and s in ctx.scopes
            for s, p in (
                ("ledger.summary.read", "allow_summary"),
                ("ledger.records.read", "allow_records"),
            )
        ):
            continue
        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": schema.model_json_schema(),
                "annotations": {
                    "readOnlyHint": name
                    not in {"ledger_create_draft", "ledger_revise_draft", "ledger_attach_source"},
                    "destructiveHint": False,
                    "openWorldHint": False,
                },
            }
        )
    snapshot = {
        "manifest_version": "ledger-agent-v2",
        "skill": skill,
        "skill_version": "1",
        "skill_hash": content_hash(skill_instructions(skill)),
        "policy_digest": POLICY_DIGEST,
        "disclosure_profile": ctx.disclosure,
        "tools": tools,
        "scopes": sorted(ctx.scopes),
        "authorization_hash": ctx.authorization_hash,
        "owner_hash": content_hash(ctx.owner_id),
        "agent_id": ctx.agent_id,
    }
    # JSON Schema contains numeric validation bounds (e.g. 0.0), not money facts.
    digest = content_hash(
        json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    )
    if persist:
        lock_command(db, ctx.owner_id, "agent.catalog", digest)
        if db.get(AgentCatalogSnapshot, digest) is None:
            db.add(
                AgentCatalogSnapshot(
                    catalog_hash=digest,
                    owner_id=ctx.owner_id,
                    agent_id=ctx.agent_id,
                    snapshot=snapshot,
                )
            )
        db.commit()
    return {
        "catalog_hash": digest,
        "skill": skill,
        "disclosure_profile": ctx.disclosure,
        "policy_digest": POLICY_DIGEST,
        "tools": tools,
        "ttl_ms": 0,
        "instructions": skill_instructions(skill),
    }


def parse_capture(spec):
    try:
        ZoneInfo(spec.timezone)
    except ZoneInfoNotFoundError as exc:
        raise AppError(422, "invalid_timezone", "时区无效") from exc
    text = spec.text
    # Only explicit currency-marked amounts. Never pick the first number from a receipt.
    matches = list(
        re.finditer(
            r"(?:[¥￥]\s*(?P<prefix>[+-]?\d+(?:\.\d{1,4})?)(?![\d.]))|(?<![\d.])(?P<suffix>[+-]?\d+(?:\.\d{1,4})?)\s*(?:元|块)(?!\w)",
            text,
        )
    )
    if not matches:
        matches = list(
            re.finditer(r"(?<![\d.])(?P<suffix>[+-]?\d+(?:\.\d{1,4})?)\s*(?:元|块)", text)
        )
    amounts = [(m, Decimal(m.groupdict().get("prefix") or m.group("suffix"))) for m in matches]
    flags, missing, evidence = [], [], {}
    if re.search(r"\d[,，]\d", text):
        flags.append("FORMATTED_AMOUNT_REQUIRES_CLARIFICATION")
    candidate = {"currency": spec.currency, "timezone": spec.timezone}
    if re.search(r"转账|还款|提现|充值|红包", text):
        flags.append("NON_CONSUMPTION_REQUIRES_CLARIFICATION")
    if re.search(r"USD|美元|EUR|欧元|HKD|港币|\$", text, re.I):
        flags.append("CURRENCY_REQUIRES_CLARIFICATION")
    if spec.currency != "CNY" and re.search(r"元|块|¥|￥", text):
        flags.append("CURRENCY_REQUIRES_CLARIFICATION")
    if len(amounts) == 1 and Decimal(0) < amounts[0][1] < Decimal("100000000000000"):
        match, amount = amounts[0]
        candidate["amount"] = format(amount, ".4f")
        evidence["amount"] = {
            "source_span": [match.start(), match.end()],
            "method": "explicit_currency_amount",
        }
    else:
        missing.append("amount")
        if amounts:
            flags.append("AMOUNT_AMBIGUOUS_OR_INVALID")
    if spec.occurred_at:
        candidate["occurred_at"] = spec.occurred_at.isoformat()
    else:
        missing.append("occurred_at")
    candidate["money_type"] = (
        "refund" if "退款" in text else "income" if "收入" in text else "expense"
    )
    labels = [
        (label, key)
        for label, key in (("微信", "wechat"), ("支付宝", "alipay"), ("现金", "cash"))
        if label in text
    ]
    if len(labels) == 1:
        label, key = labels[0]
        candidate["payment_method"] = key
        start = text.index(label)
        evidence["payment_method"] = {
            "source_span": [start, start + len(label)],
            "method": "explicit_label",
        }
    elif labels:
        flags.append("PAYMENT_METHOD_AMBIGUOUS")
    return {
        "candidate": candidate,
        "missing_fields": missing,
        "review_flags": flags,
        "field_evidence": evidence,
        "parser_version": "ledger-rule-capture-v1",
        "verifier_version": "ledger-candidate-v1",
        "input_trust": "untrusted_data",
        "ready_for_draft": not missing and not flags,
        "notice": "未保存；请核对类型、时间与金额。模型补全仍需通过草稿 schema，不允许确认。",
    }


def authored_draft(db, ctx, record_id):
    authored = db.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.owner_id == ctx.owner_id,
            AuditEvent.aggregate_type == "record",
            AuditEvent.aggregate_id == record_id,
            AuditEvent.action == "record.created",
            AuditEvent.actor_id == ctx.agent_id,
            AuditEvent.actor_type == "agent",
        )
        .limit(1)
    )
    if not authored:
        raise AppError(404, "agent_draft_not_found", "本 Agent 草稿不存在")
    record = locked_record(db, ctx.owner_id, record_id)
    if record.state != "draft" or record.record_kind != "money_only":
        raise AppError(409, "agent_draft_only", "只能修改本 Agent 未确认的金额草稿")
    return record


def draft_command(db, ctx, tool, spec):
    key = "agent-v2:" + content_hash({"agent": ctx.agent_id, "key": spec.idempotency_key})
    payload = spec.model_dump(mode="json")
    old = idempotency_lookup(db, ctx.owner_id, tool, key, payload)
    if old:
        if old.response_body:
            return {**old.response_body, "replayed": True}
        raise AppError(409, "draft_result_missing", "草稿请求结果不可用")
    if tool == "ledger_create_draft":
        data = RecordCreate(
            occurred_at=spec.occurred_at.astimezone(UTC),
            timezone=spec.timezone,
            money_entry=MoneyEntryInput(
                type=spec.money_type,
                amount=spec.amount,
                currency=spec.currency,
                title=spec.title,
                category_key=spec.category_key,
                payment_method=spec.payment_method,
            ),
            confirm=False,
        )
        record = create_record(
            db, ctx.owner_id, data, key, ctx.agent_id, actor_type="agent", commit=False
        )
        if spec.source_text:
            source = CaptureSource(
                owner_id=ctx.owner_id,
                source_type="agent_text",
                source_external_id=key,
                raw_text=spec.source_text,
                raw_payload={"input_trust": "untrusted_data"},
                parser="agent_candidate",
                parser_version="1",
                capture_state="parsed",
                captured_at=datetime.now(UTC),
            )
            db.add(source)
            db.flush()
            db.add(LedgerRecordSource(record_id=record.id, source_id=source.id, role="primary"))
    else:
        record = authored_draft(db, ctx, spec.record_id)
        if record.revision != spec.revision:
            raise AppError(409, "revision_conflict", "草稿已更新")
        if tool == "ledger_revise_draft":
            entry = record.money_entry
            category = db.get(MoneyCategory, entry.category_id) if entry.category_id else None
            values = {
                "type": entry.type,
                "amount": entry.amount,
                "currency": entry.currency,
                "title": entry.title,
                "payment_method": entry.payment_method,
                "category_key": category.key if category else None,
                "related_entry_id": entry.related_entry_id,
            }
            for name in ("amount", "title", "payment_method"):
                if name in spec.model_fields_set:
                    values[name] = getattr(spec, name)
            record = patch_record(
                db,
                ctx.owner_id,
                record.id,
                spec.revision,
                RecordPatch(money_entry=MoneyEntryInput(**values)),
                ctx.agent_id,
                commit=False,
                actor_type="agent",
            )
        else:
            source = db.get(CaptureSource, spec.source_id)
            # Source IDs alone do not grant access; it must already belong to this Agent's capture chain.
            owned_source = db.scalar(
                select(LedgerRecordSource.record_id)
                .join(AuditEvent, AuditEvent.aggregate_id == LedgerRecordSource.record_id)
                .where(
                    LedgerRecordSource.source_id == spec.source_id,
                    AuditEvent.owner_id == ctx.owner_id,
                    AuditEvent.action == "record.created",
                    AuditEvent.actor_type == "agent",
                    AuditEvent.actor_id == ctx.agent_id,
                )
                .limit(1)
            )
            if source is None or source.owner_id != ctx.owner_id or not owned_source:
                raise AppError(404, "source_not_found", "本 Agent 已授权来源不存在")
            if not db.scalar(
                select(LedgerRecordSource.record_id)
                .where(
                    LedgerRecordSource.record_id == record.id,
                    LedgerRecordSource.source_id == source.id,
                )
                .limit(1)
            ):
                db.add(
                    LedgerRecordSource(record_id=record.id, source_id=source.id, role="evidence")
                )
                record.revision += 1
    response = {
        "record_ref": f"shadow://ledger/records/{record.id}",
        "revision": record.revision,
        "state": record.state,
        "final_entry_created": False,
        "replayed": False,
    }
    idempotency_save(db, ctx.owner_id, tool, key, payload, record.id)
    db.flush()
    saved = idempotency_lookup(db, ctx.owner_id, tool, key, payload)
    saved.response_body = response
    db.add(
        AuditEvent(
            owner_id=ctx.owner_id,
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="agent.draft.command",
            aggregate_type="record",
            aggregate_id=record.id,
            details={"tool": tool},
        )
    )
    db.commit()
    return response


def call_tool(db, ctx, skill, catalog_hash, name, arguments):
    catalog = compile_catalog(db, ctx, skill, persist=False)
    if catalog_hash != catalog["catalog_hash"]:
        raise AppError(409, "tool_catalog_changed", "权限或工具目录已变化，请重新获取目录")
    if name not in {tool["name"] for tool in catalog["tools"]}:
        raise AppError(403, "tool_not_granted", "当前任务未授权此工具")
    try:
        spec = TOOLS[name][0].model_validate(arguments)
    except ValidationError as exc:
        # Never echo sensitive Pydantic input values to traces or error bodies.
        raise AppError(422, "invalid_tool_arguments", "工具参数未通过 schema 校验") from exc
    if name == "ledger_explain":
        return queries.explain(db, ctx, spec, catalog_hash)
    if name == "ledger_parse_capture":
        return parse_capture(spec)
    if name in {"ledger_create_draft", "ledger_revise_draft", "ledger_attach_source"}:
        try:
            return draft_command(db, ctx, name, spec)
        except ValidationError as exc:
            raise AppError(422, "invalid_draft_fields", "草稿字段无效，未写入变更") from exc
    handler = {
        "ledger_overview": queries.overview,
        "ledger_search": queries.search,
        "ledger_entity_insights": queries.entity_insights,
        "ledger_attention": queries.attention,
    }[name]
    result = handler(db, ctx, spec)
    return queries.persist_result(db, ctx, name, spec, result, catalog_hash)
