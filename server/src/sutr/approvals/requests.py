import uuid
from datetime import datetime, timedelta

from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from sutr.approvals.normalize import hash_normalized_args, normalize_tool_args
from sutr.approvals.summarize import summarize_tool_call
from sutr.config import settings
from sutr.models.org import Org
from sutr.models.tool_approval_request import ToolApprovalRequest


def _approval_expiry_minutes(session: Session, org_id: uuid.UUID) -> int:
    org = session.get(Org, org_id)
    if org is not None and org.approval_expiry_minutes is not None:
        return org.approval_expiry_minutes
    return settings.approval_expiry_minutes


def get_or_create_approval_request(
    session: Session,
    org_id: uuid.UUID,
    integration_id: str,
    tool_name: str,
    args: dict,
    requested_by_agent: str | None = None,
    requester_ip: str | None = None,
    user_agent: str | None = None,
    api_key_label: str | None = None,
    api_key_prefix: str | None = None,
    additional_info: str | None = None,
) -> ToolApprovalRequest:
    normalized = normalize_tool_args(args)
    args_hash = hash_normalized_args(normalized)
    now = datetime.utcnow()

    # Reuse existing pending non-expired request with same signature.
    # If the caller supplied fresh additional_info on a retry, attach it so the
    # human approver sees the latest explanation even when the underlying request
    # is reused.
    existing = session.exec(
        select(ToolApprovalRequest)
        .where(ToolApprovalRequest.org_id == org_id)
        .where(ToolApprovalRequest.integration_id == integration_id)
        .where(ToolApprovalRequest.tool_name == tool_name)
        .where(ToolApprovalRequest.args_hash == args_hash)
        .where(ToolApprovalRequest.status == "pending")
        .where(ToolApprovalRequest.expires_at > now)
    ).first()
    if existing:
        if additional_info and not existing.additional_info:
            existing.additional_info = additional_info
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return existing

    summary = summarize_tool_call(integration_id, tool_name, args)
    request = ToolApprovalRequest(
        org_id=org_id,
        integration_id=integration_id,
        tool_name=tool_name,
        args_json=normalized,
        args_hash=args_hash,
        summary_text=summary,
        status="pending",
        requested_by_agent=requested_by_agent,
        requested_at=now,
        expires_at=now + timedelta(minutes=_approval_expiry_minutes(session, org_id)),
        requester_ip=requester_ip,
        user_agent=user_agent,
        api_key_label=api_key_label,
        api_key_prefix=api_key_prefix,
        additional_info=additional_info,
    )
    session.add(request)
    session.commit()
    session.refresh(request)
    return request


def create_auto_approved_request(
    session: Session,
    org_id: uuid.UUID,
    integration_id: str,
    tool_name: str,
    args: dict,
    requested_by_agent: str | None = None,
    requester_ip: str | None = None,
    user_agent: str | None = None,
    api_key_label: str | None = None,
    api_key_prefix: str | None = None,
    additional_info: str | None = None,
) -> ToolApprovalRequest:
    """Create an already-decided request record for a policy-matched (auto-approved) call."""
    normalized = normalize_tool_args(args)
    args_hash = hash_normalized_args(normalized)
    summary = summarize_tool_call(integration_id, tool_name, args)
    now = datetime.utcnow()
    request = ToolApprovalRequest(
        org_id=org_id,
        integration_id=integration_id,
        tool_name=tool_name,
        args_json=normalized,
        args_hash=args_hash,
        summary_text=summary,
        status="auto_approved",
        decision_mode="allow_tool_forever",
        decided_at=now,
        requested_by_agent=requested_by_agent,
        requested_at=now,
        expires_at=now,
        requester_ip=requester_ip,
        user_agent=user_agent,
        api_key_label=api_key_label,
        api_key_prefix=api_key_prefix,
        additional_info=additional_info,
    )
    session.add(request)
    session.commit()
    session.refresh(request)
    return request


def try_consume_approved_request(
    session: Session,
    org_id: uuid.UUID,
    integration_id: str,
    tool_name: str,
    args_hash: str,
) -> ToolApprovalRequest | None:
    """Try to consume an approve-once request. Returns the consumed request or None.

    The transition is a conditional UPDATE keyed on status="approved", so two
    concurrent callers racing to consume the same grant resolve to exactly one
    winner — the loser sees rowcount 0 and falls back to the approval gate.
    """
    now = datetime.utcnow()
    request = session.exec(
        select(ToolApprovalRequest)
        .where(ToolApprovalRequest.org_id == org_id)
        .where(ToolApprovalRequest.integration_id == integration_id)
        .where(ToolApprovalRequest.tool_name == tool_name)
        .where(ToolApprovalRequest.args_hash == args_hash)
        .where(ToolApprovalRequest.status == "approved")
        .where(ToolApprovalRequest.decision_mode == "approve_once")
        .where(ToolApprovalRequest.expires_at > now)
    ).first()
    if not request:
        return None

    result = session.execute(
        sa_update(ToolApprovalRequest)
        .where(ToolApprovalRequest.id == request.id)  # type: ignore[arg-type]
        .where(ToolApprovalRequest.status == "approved")  # type: ignore[arg-type]
        .values(status="consumed", consumed_at=now)
    )
    session.commit()
    if result.rowcount != 1:
        return None
    session.refresh(request)
    return request


def find_exact_forever_grant(
    session: Session,
    org_id: uuid.UUID,
    integration_id: str,
    tool_name: str,
    args_hash: str,
) -> ToolApprovalRequest | None:
    """A standing approve-exact-forever grant for this exact argument hash.

    Never consumed: the human approved this tool with these exact arguments
    permanently. Expiry does not apply once approved.
    """
    return session.exec(
        select(ToolApprovalRequest)
        .where(ToolApprovalRequest.org_id == org_id)
        .where(ToolApprovalRequest.integration_id == integration_id)
        .where(ToolApprovalRequest.tool_name == tool_name)
        .where(ToolApprovalRequest.args_hash == args_hash)
        .where(ToolApprovalRequest.status == "approved")
        .where(ToolApprovalRequest.decision_mode == "approve_exact_forever")
    ).first()
