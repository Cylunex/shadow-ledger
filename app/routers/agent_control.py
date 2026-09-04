from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_schemas import ApprovalDecision, ExecuteRequest, ReviewRequest, Skill, ToolCall
from app.db import get_db
from app.errors import AppError
from app.external import is_lan_bypass
from app.machine import _grant, _require_agent
from app.models import AgentApprovalGrant, AgentExecutionReceipt, AgentIntent, AgentPolicyDecision
from app.routers.workbench import user_actor
from app.security import Actor
from app.services import agent_effects as effects
from app.services.agent_canonical import POLICY_DIGEST, aware, capability_hash
from app.services.agent_gateway import call_tool, compile_catalog, local_context, machine_context

machine = APIRouter(prefix="/api/machine/v1/agent", tags=["agent-control"])
browser = APIRouter(prefix="/api/v1/agent", tags=["agent-review"])


def human_session(request: Request, actor: Actor = Depends(user_actor)):
    if request.headers.get("Authorization") or is_lan_bypass(request):
        raise AppError(
            403,
            "human_session_required",
            "Agent 审批需要登录的 Ledger 用户会话，不接受 Bearer 或 LAN 免登录",
        )
    return actor


@machine.get("/catalog")
def catalog(
    request: Request,
    skill: Skill = "research",
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = _require_agent(request, authorization, "")
    return compile_catalog(db, machine_context(db, identity), skill)


@machine.post("/tools/call")
def gateway(
    body: ToolCall,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = _require_agent(request, authorization, "")
    return call_tool(
        db, machine_context(db, identity), body.skill, body.catalog_hash, body.tool, body.arguments
    )


@machine.post("/review-requests")
def request_review(
    body: ReviewRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    identity = _require_agent(request, authorization, "ledger.records.write")
    grant = _grant(db, identity, "allow_confirm")
    intent = effects.request_review(
        db,
        grant.owner_id,
        identity.agent_id,
        body.record_id,
        body.revision,
        body.action,
        idempotency_key,
    )
    return {
        "intent_id": str(intent.id),
        "state": intent.state,
        "args_hash": intent.args_hash,
        "review_path": f"/workbench?agent_intent={intent.id}",
        "intent_ref": f"shadow://ledger/agent-intents/{intent.id}",
    }


@machine.get("/review-requests/{intent_id}")
def review_status(
    intent_id: uuid.UUID,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    identity = _require_agent(request, authorization, "ledger.records.write")
    grant = _grant(db, identity, "allow_confirm")
    intent = effects.read_intent(db, grant.owner_id, intent_id)
    if intent.agent_id != identity.agent_id:
        raise AppError(404, "agent_intent_not_found", "审核不存在")
    approval = db.scalar(
        select(AgentApprovalGrant).where(AgentApprovalGrant.intent_id == intent.id)
    )
    return {
        "intent_id": str(intent.id),
        "state": intent.state,
        "expires_at": aware(intent.expires_at).isoformat(),
        "approval_grant_id": str(approval.id) if approval else None,
    }


@browser.get("/catalog")
def browser_catalog(
    skill: Skill = "research", actor: Actor = Depends(human_session), db: Session = Depends(get_db)
):
    return compile_catalog(
        db, local_context(actor.owner_id, allow_drafts=True, agent_id="ledger-browser"), skill
    )


@browser.post("/tools/call")
def browser_call(
    body: ToolCall, actor: Actor = Depends(human_session), db: Session = Depends(get_db)
):
    return call_tool(
        db,
        local_context(actor.owner_id, allow_drafts=True, agent_id="ledger-browser"),
        body.skill,
        body.catalog_hash,
        body.tool,
        body.arguments,
    )


@browser.get("/reviews")
def reviews(
    state: str = Query(default="pending", pattern="^(pending|all)$"),
    offset: int = Query(default=0, ge=0, le=10000),
    actor: Actor = Depends(human_session),
    db: Session = Depends(get_db),
):
    stmt = select(AgentIntent).where(AgentIntent.owner_id == actor.owner_id)
    if state == "pending":
        stmt = stmt.where(
            AgentIntent.state.in_(["awaiting_human", "approved"]),
            AgentIntent.expires_at > datetime.now(UTC),
        )
    rows = list(
        db.scalars(
            stmt.order_by(AgentIntent.created_at.desc(), AgentIntent.id).offset(offset).limit(31)
        )
    )
    return {
        "items": [
            {
                "id": str(r.id),
                "agent_id": r.agent_id,
                "record_id": str(r.record_id),
                "revision": r.revision,
                "action": r.action,
                "state": r.state,
            }
            for r in rows[:30]
        ],
        "next_offset": offset + 30 if len(rows) > 30 else None,
    }


@browser.get("/reviews/{intent_id}")
def review(
    intent_id: uuid.UUID, actor: Actor = Depends(human_session), db: Session = Depends(get_db)
):
    intent = effects.read_intent(db, actor.owner_id, intent_id)
    decision = db.scalar(
        select(AgentPolicyDecision)
        .where(AgentPolicyDecision.intent_id == intent.id)
        .order_by(AgentPolicyDecision.created_at.desc(), AgentPolicyDecision.id)
        .limit(1)
    )
    approval = db.scalar(
        select(AgentApprovalGrant).where(AgentApprovalGrant.intent_id == intent.id)
    )
    receipt = db.scalar(
        select(AgentExecutionReceipt).where(AgentExecutionReceipt.intent_id == intent.id)
    )
    problem = None
    if intent.state in {"awaiting_human", "approved"}:
        try:
            standing = effects.standing_grant(db, actor.owner_id, intent.agent_id)
            effects.check_frozen(db, intent)
            if approval and (aware(approval.expires_at) <= datetime.now(UTC)):
                problem = "approval_expired"
            elif approval and (
                approval.policy_digest != POLICY_DIGEST
                or approval.capability_hash != capability_hash(standing)
            ):
                problem = "approval_invalidated"
        except AppError as exc:
            problem = exc.code
    return {
        "id": str(intent.id),
        "agent_id": intent.agent_id,
        "action": intent.action,
        "state": intent.state,
        "revision": intent.revision,
        "snapshot": intent.snapshot,
        "display_hash": intent.args_hash,
        "expires_at": aware(intent.expires_at).isoformat(),
        "problem": problem,
        "policy": {
            "verdict": decision.verdict,
            "reason_codes": decision.reason_codes,
            "digest": decision.policy_digest,
        }
        if decision
        else None,
        "approval_grant_id": str(approval.id) if approval else None,
        "approval_expires_at": aware(approval.expires_at).isoformat() if approval else None,
        "receipt": effects.receipt_result(receipt) if receipt else None,
    }


@browser.post("/reviews/{intent_id}/decision")
def decision(
    intent_id: uuid.UUID,
    body: ApprovalDecision,
    actor: Actor = Depends(human_session),
    db: Session = Depends(get_db),
):
    return effects.approve(db, actor.owner_id, intent_id, body.display_hash, body.accept)


@browser.post("/execute")
def execute(
    body: ExecuteRequest, actor: Actor = Depends(human_session), db: Session = Depends(get_db)
):
    approval = db.get(AgentApprovalGrant, body.approval_grant_id)
    if approval is None or approval.owner_id != actor.owner_id:
        raise AppError(404, "approval_not_found", "批准不存在")
    intent = effects.read_intent(db, actor.owner_id, approval.intent_id)
    return effects.execute(db, actor.owner_id, intent.agent_id, approval.id)


@browser.get("/receipts/{receipt_id}")
def receipt(
    receipt_id: uuid.UUID, actor: Actor = Depends(human_session), db: Session = Depends(get_db)
):
    receipt = db.get(AgentExecutionReceipt, receipt_id)
    if receipt is None or receipt.owner_id != actor.owner_id:
        raise AppError(404, "receipt_not_found", "执行凭证不存在")
    return effects.receipt_result(receipt)
