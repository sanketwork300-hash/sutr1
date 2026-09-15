"""Usage quotas — the limits, not the counts.

A quota is a limit row; consumption is derived from the usage ledger, so this
resource has no counters to keep in sync. `GET` returns each configured limit
alongside its current consumption so an operator can see both in one place.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from sutr.authz import ensure_agent_can
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.models.quota import (
    CONCURRENT_TOOL_CALLS,
    QUOTA_KINDS,
    SCOPE_INTEGRATION,
    SCOPE_TENANT,
    SCOPE_TOOL,
    SCOPES,
    Quota,
)
from sutr.services import quota as quota_service
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/api/quotas", tags=["quotas"])


class QuotaRequest(BaseModel):
    kind: str
    limit_value: int = Field(ge=0)
    scope: str = SCOPE_TENANT
    scope_id: str = ""
    enabled: bool = True


def _validate(body: QuotaRequest) -> None:
    if body.kind not in QUOTA_KINDS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_quota_kind",
                "message": f"kind must be one of {list(QUOTA_KINDS)}",
            },
        )
    if body.scope not in SCOPES:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_scope", "message": f"scope must be one of {list(SCOPES)}"},
        )
    if body.scope == SCOPE_TENANT and body.scope_id:
        raise HTTPException(
            status_code=400,
            detail="A tenant-scoped quota takes no scope_id.",
        )
    if body.scope in (SCOPE_INTEGRATION, SCOPE_TOOL) and not body.scope_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "scope_id is required: the integration id, or '<integration>/<tool>' "
                "for a tool-scoped quota."
            ),
        )
    if body.scope == SCOPE_TOOL and "/" not in body.scope_id:
        raise HTTPException(
            status_code=400,
            detail="A tool-scoped quota's scope_id must be '<integration_id>/<tool_name>'.",
        )


def _serialize(session: Session, quota: Quota) -> dict:
    data = {
        "id": str(quota.id),
        "kind": quota.kind,
        "scope": quota.scope,
        "scope_id": quota.scope_id,
        "limit_value": quota.limit_value,
        "enabled": quota.enabled,
        "updated_at": quota.updated_at.isoformat(),
    }
    if quota.kind == CONCURRENT_TOOL_CALLS:
        data["used"] = quota_service.concurrency.current(quota.org_id)
        data["window"] = "instantaneous"
    else:
        integration_id, _, tool_name = quota.scope_id.partition("/")
        used = quota_service._used(session, quota, integration_id, tool_name, quota_service._now())
        data["used"] = used if used >= 0 else None
        data["window"] = "day" if quota.kind.startswith("daily") else "month"
        if used < 0:
            data["enforceable"] = False
            data["note"] = (
                "This metric is not recorded per call yet, so the limit is stored but not enforced."
            )
    return data


@router.get("")
def list_quotas(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    ensure_agent_can(session, agent_auth, "logs:read")
    quotas = session.exec(select(Quota).where(Quota.org_id == agent_auth.org.id)).all()
    return [_serialize(session, quota) for quota in quotas]


@router.put("")
def set_quota(
    body: QuotaRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Create or replace one limit. Idempotent on (kind, scope, scope_id)."""
    ensure_agent_can(session, agent_auth, "org:settings:write")
    _validate(body)

    quota = session.exec(
        select(Quota)
        .where(Quota.org_id == agent_auth.org.id)
        .where(Quota.kind == body.kind)
        .where(Quota.scope == body.scope)
        .where(Quota.scope_id == body.scope_id)
    ).first()
    if quota is None:
        quota = Quota(org_id=agent_auth.org.id, kind=body.kind, scope=body.scope)
    quota.scope_id = body.scope_id
    quota.limit_value = body.limit_value
    quota.enabled = body.enabled
    quota.updated_at = datetime.utcnow()
    session.add(quota)

    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="quota.set",
        summary=(
            f"Quota '{body.kind}' set to {body.limit_value} for "
            f"{body.scope}{f' {body.scope_id}' if body.scope_id else ''}"
        ),
        target_type="quota",
        target_id=f"{body.kind}:{body.scope}:{body.scope_id}",
        metadata={"limit_value": body.limit_value, "enabled": body.enabled},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(quota)
    return _serialize(session, quota)


@router.delete("/{quota_id}", status_code=204)
def delete_quota(
    quota_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    ensure_agent_can(session, agent_auth, "org:settings:write")
    quota = session.get(Quota, quota_id)
    if quota is None or quota.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="Quota not found")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="quota.removed",
        summary=f"Quota '{quota.kind}' removed",
        target_type="quota",
        target_id=f"{quota.kind}:{quota.scope}:{quota.scope_id}",
        **actor_from_agent_auth(agent_auth),
    )
    session.delete(quota)
    session.commit()
