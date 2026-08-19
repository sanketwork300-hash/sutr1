"""Control-plane audit trail writer.

`record_audit` adds an AuditEvent to the caller's session WITHOUT committing —
the audit row must land in the same transaction as the action it describes,
so an action can never commit without its trail (and vice versa). Callers
commit as usual right after.
"""

import json
import uuid

from fastapi import Request
from sqlmodel import Session

from sutr.models.audit_event import AuditEvent


def request_meta(request: Request | None) -> tuple[str | None, str | None]:
    """(ip, user_agent) from a FastAPI request, tolerant of None."""
    if request is None:
        return None, None
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


def record_user_security_audit(
    session: Session,
    user,
    action: str,
    summary: str,
    request: Request | None = None,
) -> None:
    """Audit an account-security event under the user's default org.

    These events (password change, TOTP toggles) are user-scoped rather than
    org-scoped requests; the default membership picks where the trail lives.
    Skipped silently when the user has no membership (cannot happen through
    normal signup, but never block a security action on audit placement).
    """
    from sutr.dependencies import default_membership

    membership = default_membership(session, user.id)
    if membership is None:
        return
    record_audit(
        session,
        org_id=membership.org_id,
        action=action,
        summary=summary,
        actor_user_id=user.id,
        request=request,
    )


def actor_from_agent_auth(auth) -> dict:
    """record_audit kwargs for an AgentAuth caller (user JWT or API key)."""
    return {
        "actor_user_id": auth.user.id if auth.user else None,
        "actor_api_key_prefix": auth.api_key.key_prefix if auth.api_key else None,
        "impersonator_user_id": auth.impersonator.id if auth.impersonator is not None else None,
    }


def record_audit(
    session: Session,
    *,
    org_id: uuid.UUID,
    action: str,
    summary: str,
    actor_user_id: uuid.UUID | None = None,
    actor_api_key_prefix: str | None = None,
    actor_type: str | None = None,
    impersonator_user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
    request: Request | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditEvent:
    if request is not None and ip is None and user_agent is None:
        ip, user_agent = request_meta(request)
    if actor_type is None:
        actor_type = "api_key" if actor_api_key_prefix and not actor_user_id else "user"
    event = AuditEvent(
        org_id=org_id,
        action=action,
        summary=summary,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        actor_api_key_prefix=actor_api_key_prefix,
        impersonator_user_id=impersonator_user_id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata_json=json.dumps(metadata or {}),
        ip=ip,
        user_agent=user_agent,
    )
    session.add(event)
    return event
