import uuid
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import update as sa_update
from sqlmodel import Session, col, select

from sutr.analytics import posthog_client
from sutr.api.schemas import AwaitApprovalRequest, AwaitApprovalResponse
from sutr.api.second_factor import require_second_factor
from sutr.approvals import events as approval_events
from sutr.authz import require_permission
from sutr.config import settings
from sutr.db import engine, get_session
from sutr.dependencies import AgentAuth, get_agent_auth, get_current_org, get_current_user
from sutr.models.log import LogEntry
from sutr.models.org import Org
from sutr.models.tool_approval_request import ToolApprovalRequest
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.models.user import User
from sutr.services.audit import record_audit

router = APIRouter(prefix="/api/tool-approvals", tags=["tool-approvals"])


def _update_pending_log(session: Session, approval_request_id: uuid.UUID, outcome: str) -> None:
    """Find the pending LogEntry for this approval request and transition its outcome."""
    log = session.exec(
        select(LogEntry)
        .where(LogEntry.approval_request_id == approval_request_id)
        .where(col(LogEntry.outcome).in_(["pending", "approval_required"]))
    ).first()
    if log:
        log.outcome = outcome
        session.add(log)


def _effective_status(req: ToolApprovalRequest) -> str:
    if req.status == "pending" and req.expires_at <= datetime.utcnow():
        return "expired"
    return req.status


def _await_message(status: str) -> str:
    if status == "approved":
        return "Approved. Retry the original tool call to execute it."
    if status == "denied":
        return "This tool call was denied by the human."
    if status == "pending":
        return "Still pending — the human hasn't decided yet."
    if status == "expired":
        return "Approval request expired before it was approved."
    if status == "consumed":
        return "This approval was already consumed. Retry the original tool call to start over."
    if status == "auto_approved":
        return "This request is already auto-approved. Retry the original tool call instead."
    return f"Approval request is '{status}'. Retry the original tool call to start over."


def _build_await_response(req: ToolApprovalRequest, status: str) -> AwaitApprovalResponse:
    return AwaitApprovalResponse(
        approval_request_id=req.id,
        integration_id=req.integration_id,
        tool_name=req.tool_name,
        status=status,
        message=_await_message(status),
        expires_at=req.expires_at,
        decision_mode=req.decision_mode,
    )


@router.get("/requests")
def list_requests(
    status: str | None = None,
    integration_id: str | None = None,
    tool_name: str | None = None,
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
) -> list[dict]:
    stmt = (
        select(ToolApprovalRequest)
        .where(ToolApprovalRequest.org_id == current_org.id)
        .order_by(col(ToolApprovalRequest.requested_at).desc())
    )
    if status:
        stmt = stmt.where(ToolApprovalRequest.status == status)
    if integration_id:
        stmt = stmt.where(ToolApprovalRequest.integration_id == integration_id)
    if tool_name:
        stmt = stmt.where(ToolApprovalRequest.tool_name == tool_name)
    stmt = stmt.offset(offset).limit(limit)
    requests = session.exec(stmt).all()
    return [r.model_dump() for r in requests]


@router.get("/requests/{request_id}")
def get_request(
    request_id: uuid.UUID,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
) -> dict:
    req = session.get(ToolApprovalRequest, request_id)
    if not req or req.org_id != current_org.id:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return req.model_dump()


@router.post("/requests/{request_id}/await", response_model=AwaitApprovalResponse)
async def await_request(
    request_id: uuid.UUID,
    body: AwaitApprovalRequest | None = Body(default=None),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> AwaitApprovalResponse:
    timeout_seconds = float(settings.approval_long_poll_timeout_seconds)
    if body and body.timeout_seconds is not None:
        timeout_seconds = min(timeout_seconds, float(body.timeout_seconds))

    org_id = agent_auth.org.id

    with Session(engine) as session:
        req = session.get(ToolApprovalRequest, request_id)
        if not req or req.org_id != org_id:
            raise HTTPException(status_code=404, detail="Approval request not found")
        status = _effective_status(req)

    if status == "pending":

        def _peek_status() -> str | None:
            with Session(engine) as peek_session:
                current = peek_session.get(ToolApprovalRequest, request_id)
                if not current or current.org_id != org_id:
                    return None
                return _effective_status(current)

        await approval_events.wait_for_decision(
            request_id,
            timeout=timeout_seconds,
            pre_check=_peek_status,
        )

        with Session(engine) as session:
            req = session.get(ToolApprovalRequest, request_id)
            if not req or req.org_id != org_id:
                raise HTTPException(status_code=404, detail="Approval request not found")
            status = _effective_status(req)

    return _build_await_response(req, status)


def _load_pending_request(
    session: Session, request_id: uuid.UUID, org_id: uuid.UUID
) -> ToolApprovalRequest:
    """Load a request for a decision: 404 unknown, 409 decided, 410 expired."""
    req = session.get(ToolApprovalRequest, request_id)
    if not req or req.org_id != org_id:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail=f"Request is already '{req.status}'")
    if req.expires_at <= datetime.utcnow():
        req.status = "expired"
        session.add(req)
        session.commit()
        raise HTTPException(status_code=410, detail="Approval request has expired")
    return req


def _transition_pending(session: Session, request_id: uuid.UUID, now: datetime, **values) -> None:
    """Atomically transition a request out of `pending`.

    A single conditional UPDATE closes the check-then-act window between two
    concurrent decisions (approve vs deny, double-approve): exactly one caller
    observes rowcount == 1; the loser gets a 409 instead of silently
    overwriting the winner's decision.
    """
    result = session.execute(
        sa_update(ToolApprovalRequest)
        .where(ToolApprovalRequest.id == request_id)  # type: ignore[arg-type]
        .where(ToolApprovalRequest.status == "pending")  # type: ignore[arg-type]
        .where(ToolApprovalRequest.expires_at > now)  # type: ignore[arg-type]
        .values(**values)
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="Request was decided concurrently or expired")


def _decide(
    *,
    session: Session,
    http_request: Request,
    current_user: User,
    current_org: Org,
    request_id: uuid.UUID,
    totp_code: str | None,
    status: str,
    decision_mode: str,
    log_outcome: str,
    audit_action: str,
    posthog_event: str,
    policy_created: bool = False,
) -> ToolApprovalRequest:
    req = _load_pending_request(session, request_id, current_org.id)

    require_second_factor(current_user, totp_code)
    session.add(current_user)

    now = datetime.utcnow()
    approver_ip = http_request.client.host if http_request.client else None
    _transition_pending(
        session,
        request_id,
        now,
        status=status,
        decision_mode=decision_mode,
        decided_by_user_id=current_user.id,
        decided_at=now,
        approver_ip=approver_ip,
        policy_created=policy_created,
    )
    _update_pending_log(session, request_id, log_outcome)
    record_audit(
        session,
        org_id=current_org.id,
        action=audit_action,
        summary=f"{req.tool_name} on {req.integration_id}: {decision_mode}",
        actor_user_id=current_user.id,
        target_type="approval_request",
        target_id=str(request_id),
        metadata={
            "integration_id": req.integration_id,
            "tool_name": req.tool_name,
            "decision_mode": decision_mode,
        },
        request=http_request,
    )
    session.commit()
    session.refresh(req)
    approval_events.notify_decision(request_id, req.status)
    posthog_client.capture(
        distinct_id=str(current_user.id),
        event=posthog_event,
        properties={"integration_id": req.integration_id, "tool_name": req.tool_name},
    )
    return req


@router.post("/requests/{request_id}/approve-once")
def approve_once(
    request_id: uuid.UUID,
    http_request: Request,
    totp_code: str | None = Body(default=None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("approvals:decide")),
) -> dict:
    req = _decide(
        session=session,
        http_request=http_request,
        current_user=current_user,
        current_org=current_org,
        request_id=request_id,
        totp_code=totp_code,
        status="approved",
        decision_mode="approve_once",
        log_outcome="approved",
        audit_action="approval.approved_once",
        posthog_event="tool_approval_approved_once",
    )
    return req.model_dump()


@router.post("/requests/{request_id}/approve-exact")
def approve_exact_forever(
    request_id: uuid.UUID,
    http_request: Request,
    totp_code: str | None = Body(default=None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("approvals:decide")),
) -> dict:
    """Approve this tool with these EXACT arguments, forever.

    Unlike approve-once the grant is never consumed: future calls whose
    normalized argument hash matches execute without a new approval, and are
    logged with access_reason="approved_exact". Any change to the arguments
    goes back through the approval gate.
    """
    req = _decide(
        session=session,
        http_request=http_request,
        current_user=current_user,
        current_org=current_org,
        request_id=request_id,
        totp_code=totp_code,
        status="approved",
        decision_mode="approve_exact_forever",
        log_outcome="approved",
        audit_action="approval.approved_exact_forever",
        posthog_event="tool_approval_approved_exact_forever",
    )
    return req.model_dump()


@router.post("/requests/{request_id}/allow-tool")
def allow_tool(
    request_id: uuid.UUID,
    http_request: Request,
    totp_code: str | None = Body(default=None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("approvals:decide")),
) -> dict:
    """Approve all future calls to this tool regardless of parameters."""
    req = _load_pending_request(session, request_id, current_org.id)

    require_second_factor(current_user, totp_code)
    session.add(current_user)

    now = datetime.utcnow()

    # Upsert execution setting to "allow" — this is the single source of truth for the
    # tool's policy and is what the ModeControl in the UI reflects.
    existing_setting = session.exec(
        select(ToolExecutionSetting)
        .where(ToolExecutionSetting.org_id == current_org.id)
        .where(ToolExecutionSetting.integration_id == req.integration_id)
        .where(ToolExecutionSetting.tool_name == req.tool_name)
    ).first()
    old_mode = existing_setting.mode if existing_setting else "require_approval"
    if existing_setting:
        existing_setting.mode = "allow"
        existing_setting.updated_by_user_id = current_user.id
        existing_setting.updated_at = now
        session.add(existing_setting)
    else:
        session.add(
            ToolExecutionSetting(
                org_id=current_org.id,
                integration_id=req.integration_id,
                tool_name=req.tool_name,
                mode="allow",
                updated_by_user_id=current_user.id,
                updated_at=now,
            )
        )

    approver_ip = http_request.client.host if http_request.client else None
    _transition_pending(
        session,
        request_id,
        now,
        status="approved",
        decision_mode="allow_tool_forever",
        policy_created=True,
        decided_by_user_id=current_user.id,
        decided_at=now,
        approver_ip=approver_ip,
    )
    _update_pending_log(session, request_id, "approved")
    record_audit(
        session,
        org_id=current_org.id,
        action="approval.allowed_forever",
        summary=f"{req.tool_name} on {req.integration_id}: allow_tool_forever",
        actor_user_id=current_user.id,
        target_type="approval_request",
        target_id=str(request_id),
        metadata={
            "integration_id": req.integration_id,
            "tool_name": req.tool_name,
            "old_mode": old_mode,
            "new_mode": "allow",
        },
        request=http_request,
    )
    session.commit()
    session.refresh(req)
    approval_events.notify_decision(request_id, req.status)
    posthog_client.capture(
        distinct_id=str(current_user.id),
        event="tool_approval_allowed_forever",
        properties={"integration_id": req.integration_id, "tool_name": req.tool_name},
    )
    return req.model_dump()


@router.post("/requests/{request_id}/deny")
def deny_request(
    request_id: uuid.UUID,
    http_request: Request,
    totp_code: str | None = Body(default=None, embed=True),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    _=Depends(require_permission("approvals:decide")),
) -> dict:
    req = _decide(
        session=session,
        http_request=http_request,
        current_user=current_user,
        current_org=current_org,
        request_id=request_id,
        totp_code=totp_code,
        status="denied",
        decision_mode="deny",
        log_outcome="denied",
        audit_action="approval.denied",
        posthog_event="tool_approval_denied",
    )
    return req.model_dump()
