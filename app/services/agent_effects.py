"""Human authorization is stored here, never inferred from an Agent token."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import (
    AgentApprovalGrant,
    AgentExecutionReceipt,
    AgentIntent,
    AgentPolicyDecision,
    AuditEvent,
    LedgerAgentGrant,
    LedgerRecord,
)
from app.services.agent_canonical import (
    POLICY_DIGEST,
    POLICY_VERSION,
    aware,
    capability_hash,
    content_hash,
)
from app.services.records import (
    _confirm_locked,
    delete_draft,
    lock_command,
    record_query,
    serialize_record,
)


def standing_grant(db: Session, owner_id: str, agent_id: str):
    grant = db.scalar(
        select(LedgerAgentGrant)
        .where(LedgerAgentGrant.owner_id == owner_id, LedgerAgentGrant.agent_id == agent_id)
        .with_for_update()
    )
    if grant is None:
        raise AppError(404, "ledger_grant_not_found", "Agent 授权不存在")
    if not grant.active or not grant.allow_confirm:
        raise AppError(403, "review_permission_revoked", "Agent 审核资格已撤回")
    return grant


def locked_record(db: Session, owner_id: str, record_id: uuid.UUID):
    record = db.scalar(
        record_query()
        .where(LedgerRecord.owner_id == owner_id, LedgerRecord.id == record_id)
        .with_for_update(of=LedgerRecord)
        .execution_options(populate_existing=True)
    )
    if record is None:
        raise AppError(404, "record_not_found", "草稿不存在或已删除")
    return record


def display_snapshot(db: Session, record: LedgerRecord) -> dict:
    snapshot = serialize_record(db, record)
    snapshot["occurred_at"] = aware(record.occurred_at).isoformat()
    if snapshot["money_entry"]:
        snapshot["money_entry"]["amount"] = format(record.money_entry.amount, ".4f")
    if snapshot["consumption"]:
        lines = snapshot["consumption"]["lines"]
        for line in lines:
            for key in ("quantity", "amount"):
                if line[key] is not None:
                    line[key] = format(Decimal(line[key]), ".4f")
        lines.sort(key=lambda line: (line["sort_order"], line["id"]))
    # Sets of references are unordered; line order and raw Unicode are meaningful.
    for key in ("sources", "assets", "external_references"):
        if isinstance(snapshot.get(key), list):
            snapshot[key].sort(key=content_hash)
    return snapshot


def args_hash(action: str, snapshot: dict) -> str:
    return content_hash({"schema": "ledger-effect-v1", "action": action, "record": snapshot})


def request_review(
    db: Session,
    owner_id: str,
    agent_id: str,
    record_id: uuid.UUID,
    revision: int,
    action: str,
    key: str,
) -> AgentIntent:
    grant = standing_grant(db, owner_id, agent_id)
    if not key or len(key) > 200 or action not in {"confirm", "reject"}:
        raise AppError(422, "invalid_review_request", "审核参数无效")
    request_key = content_hash(key)
    lock_command(db, owner_id, "agent.intent", request_key)
    existing = db.scalar(
        select(AgentIntent).where(
            AgentIntent.owner_id == owner_id,
            AgentIntent.agent_id == agent_id,
            AgentIntent.request_key == request_key,
        )
    )
    if existing:
        if (existing.record_id, existing.revision, existing.action) != (
            record_id,
            revision,
            action,
        ):
            raise AppError(409, "idempotency_mismatch", "审核请求幂等键内容不同")
        return existing
    authored = db.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.owner_id == owner_id,
            AuditEvent.aggregate_type == "record",
            AuditEvent.aggregate_id == record_id,
            AuditEvent.action == "record.created",
            AuditEvent.actor_type == "agent",
            AuditEvent.actor_id == agent_id,
        )
        .limit(1)
    )
    if not authored:
        raise AppError(404, "agent_draft_not_found", "本 Agent 草稿不存在")
    record = locked_record(db, owner_id, record_id)
    if record.revision != revision or record.state != "draft":
        raise AppError(409, "revision_conflict", "请重新读取当前草稿版本")
    snapshot = display_snapshot(db, record)
    intent = AgentIntent(
        owner_id=owner_id,
        agent_id=agent_id,
        request_key=request_key,
        record_id=record_id,
        revision=revision,
        action=action,
        snapshot=snapshot,
        args_hash=args_hash(action, snapshot),
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db.add(intent)
    db.flush()
    db.add(
        AgentPolicyDecision(
            intent_id=intent.id,
            verdict="escalate",
            reason_codes=["REQUIRES_HUMAN_CONFIRMATION"],
            policy_digest=POLICY_DIGEST,
            state_hash=capability_hash(grant),
        )
    )
    db.commit()
    return intent


def read_intent(db: Session, owner_id: str, intent_id: uuid.UUID, *, locked=False):
    stmt = select(AgentIntent).where(AgentIntent.owner_id == owner_id, AgentIntent.id == intent_id)
    intent = db.scalar(stmt.with_for_update() if locked else stmt)
    if intent is None:
        raise AppError(404, "agent_intent_not_found", "审核请求不存在")
    return intent


def check_frozen(db: Session, intent: AgentIntent):
    if aware(intent.expires_at) <= datetime.now(UTC):
        raise AppError(409, "approval_expired", "审核已过期，请重新发起")
    record = locked_record(db, intent.owner_id, intent.record_id)
    if record.revision != intent.revision or record.state != "draft":
        raise AppError(409, "approval_superseded", "草稿已变化，原批准不能继续使用")
    if args_hash(intent.action, display_snapshot(db, record)) != intent.args_hash:
        raise AppError(409, "approval_content_changed", "草稿内容或证据与审核快照不一致")
    if args_hash(intent.action, intent.snapshot) != intent.args_hash:
        raise AppError(409, "approval_integrity_error", "审核内容校验失败")
    return record


def approve(db: Session, owner_id: str, intent_id: uuid.UUID, displayed_hash: str, accept: bool):
    initial = read_intent(db, owner_id, intent_id)
    standing = standing_grant(db, owner_id, initial.agent_id)
    intent = read_intent(db, owner_id, intent_id, locked=True)
    if displayed_hash != intent.args_hash:
        raise AppError(409, "approval_display_mismatch", "用户审核的内容与请求不一致")
    if intent.state in {"rejected", "executed"}:
        raise AppError(409, "approval_closed", "该审核已经结束")
    check_frozen(db, intent)
    if not accept:
        if intent.state != "awaiting_human":
            raise AppError(409, "approval_closed", "已批准的动作不可改为拒绝，请等待过期或修正草稿")
        intent.state = "rejected"
        db.add(
            AgentPolicyDecision(
                intent_id=intent.id,
                verdict="block",
                reason_codes=["HUMAN_REJECTED"],
                policy_digest=POLICY_DIGEST,
                state_hash=capability_hash(standing),
            )
        )
        db.commit()
        return {"state": "rejected", "approval_grant_id": None}
    old = db.scalar(select(AgentApprovalGrant).where(AgentApprovalGrant.intent_id == intent.id))
    if old:
        if aware(old.expires_at) <= datetime.now(UTC) or old.capability_hash != capability_hash(
            standing
        ):
            raise AppError(409, "approval_expired", "批准已失效，请创建新的审核请求")
        return {
            "state": "approved",
            "approval_grant_id": str(old.id),
            "expires_at": aware(old.expires_at).isoformat(),
        }
    decision = AgentPolicyDecision(
        intent_id=intent.id,
        verdict="allow",
        reason_codes=["HUMAN_APPROVED_EXACT_CONTENT"],
        policy_digest=POLICY_DIGEST,
        state_hash=capability_hash(standing),
    )
    db.add(decision)
    db.flush()
    grant = AgentApprovalGrant(
        intent_id=intent.id,
        decision_id=decision.id,
        owner_id=owner_id,
        approved_by=owner_id,
        args_hash=intent.args_hash,
        policy_digest=POLICY_DIGEST,
        capability_hash=capability_hash(standing),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(grant)
    intent.state = "approved"
    db.commit()
    return {
        "state": "approved",
        "approval_grant_id": str(grant.id),
        "expires_at": aware(grant.expires_at).isoformat(),
    }


def receipt_result(receipt: AgentExecutionReceipt, replayed=False):
    if content_hash(receipt.payload) != receipt.content_hash:
        raise AppError(409, "receipt_integrity_error", "执行凭证校验失败")
    return {
        "receipt": f"shadow://ledger/receipts/{receipt.id}",
        "content_hash": receipt.content_hash,
        "replayed": replayed,
        **receipt.payload,
    }


def execute(
    db: Session,
    owner_id: str,
    agent_id: str,
    grant_id: uuid.UUID,
    *,
    record_id: uuid.UUID | None = None,
    revision: int | None = None,
    action: str | None = None,
):
    standing = standing_grant(db, owner_id, agent_id)
    grant = db.scalar(
        select(AgentApprovalGrant)
        .where(AgentApprovalGrant.owner_id == owner_id, AgentApprovalGrant.id == grant_id)
        .with_for_update()
    )
    if grant is None:
        raise AppError(404, "approval_not_found", "批准不存在")
    intent = read_intent(db, owner_id, grant.intent_id, locked=True)
    if intent.agent_id != agent_id:
        raise AppError(404, "approval_not_found", "批准不存在")
    if (
        (record_id is not None and record_id != intent.record_id)
        or (revision is not None and revision != intent.revision)
        or (action is not None and action != intent.action)
    ):
        raise AppError(409, "approval_binding_mismatch", "批准与执行动作不一致")
    old = db.scalar(select(AgentExecutionReceipt).where(AgentExecutionReceipt.grant_id == grant.id))
    if old:
        return receipt_result(old, True)
    if (
        grant.consumed_at
        or intent.state != "approved"
        or grant.policy_digest != POLICY_DIGEST
        or grant.args_hash != intent.args_hash
        or grant.capability_hash != capability_hash(standing)
    ):
        raise AppError(409, "approval_invalidated", "批准已消费或权限/策略已变化")
    if aware(grant.expires_at) <= datetime.now(UTC):
        raise AppError(409, "approval_expired", "批准已过期")
    record = check_frozen(db, intent)
    timestamp = datetime.now(UTC)
    grant.consumed_at = timestamp
    if intent.action == "confirm":
        _confirm_locked(db, record, grant.approved_by, "user")
        after_revision = record.revision
    else:
        delete_draft(db, owner_id, record.id, intent.revision, commit=False)
        after_revision = None
    intent.state = "executed"
    receipt = AgentExecutionReceipt(
        owner_id=owner_id,
        agent_id=agent_id,
        intent_id=intent.id,
        grant_id=grant.id,
        payload={
            "schema_version": "ledger-receipt-v1",
            "intent_id": str(intent.id),
            "grant_id": str(grant.id),
            "action": intent.action,
            "args_hash": intent.args_hash,
            "record_ref": f"shadow://ledger/records/{intent.record_id}",
            "before_revision": intent.revision,
            "revision": after_revision,
            "actor_id": grant.approved_by,
            "agent_id": agent_id,
            "executor_id": "ledger-trusted-executor",
            "granted_scope": "ledger.records.write",
            "policy_version": POLICY_VERSION,
            "policy_digest": POLICY_DIGEST,
            "state": "confirmed" if intent.action == "confirm" else "rejected",
            "final_entry_created": intent.action == "confirm",
            "outcome": "succeeded",
            "executed_at": timestamp.isoformat(),
            "request_key_hash": intent.request_key,
        },
    )
    receipt.content_hash = content_hash(receipt.payload)
    db.add(receipt)
    db.flush()
    db.add(
        AuditEvent(
            owner_id=owner_id,
            actor_type="user",
            actor_id=grant.approved_by,
            action="agent.effect.executed",
            aggregate_type="agent_intent",
            aggregate_id=intent.id,
            details={"receipt_id": str(receipt.id), "agent_id": agent_id, "action": intent.action},
        )
    )
    db.commit()
    return receipt_result(receipt)
