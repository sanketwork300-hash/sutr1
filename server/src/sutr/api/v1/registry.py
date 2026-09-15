"""The Registry: the authoritative record for a tenant's tools (LLD §3.7).

Everything here is scoped to the caller's organization, on every route. A tool
belonging to another tenant reads as 404, never 403 — the registry holds
providers' unreleased work, and a 403 would confirm that a given tool key is
taken by somebody.

Four operations are gated: entering review, publishing, changing visibility and
changing price. Those return `201` with a **change request** and change
nothing; a second person decides them. Everything else applies immediately.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import ConflictError, InvalidRequestError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.generation import artifacts
from sutr.models.registry_change_request import STATUS_PENDING
from sutr.models.registry_tool import RegistryTool
from sutr.registry import events as registry_events
from sutr.registry import lifecycle, pricing, service, trust, versions
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/registry", tags=["registry"])

# Registering and versioning a tool is the same class of act as compiling one.
MANAGE = "integrations:manage"
# Deciding a governance request is the same class of act as deciding an
# approval request, so it holds the same permission rather than a parallel one.
DECIDE = service.DECIDE_PERMISSION
READ = "logs:read"


class RegisterRequest(BaseModel):
    tool_key: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    summary: str = ""
    description: str = ""
    category: str = "other"
    tags: list[str] = []
    # Declared by the provider. Nothing verifies them, and the response says so.
    regions: list[str] = []
    compliance: list[str] = []
    project_id: uuid.UUID | None = None
    integration_id: str | None = None


class UpdateRequest(BaseModel):
    name: str | None = None
    summary: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    regions: list[str] | None = None
    compliance: list[str] | None = None


class TransitionRequest(BaseModel):
    to: str
    reason: str = ""


class VersionRequest(BaseModel):
    artifact_id: uuid.UUID | None = None
    notes: str = ""


class VisibilityRequest(BaseModel):
    visibility: str
    reason: str = ""


class PricingRequest(BaseModel):
    model: str
    amount_micros: int = 0
    unit: str = "call"
    currency: str = "USD"
    free_allowance: int = 0
    notes: str = ""
    reason: str = ""


class DecisionRequest(BaseModel):
    approve: bool
    note: str = ""


class DeprecateRequest(BaseModel):
    note: str


def _load(session: Session, tool_id: uuid.UUID, org_id: uuid.UUID) -> RegistryTool:
    tool = service.get(session, tool_id, org_id)
    if tool is None:
        raise NotFoundError(f"Registry tool {tool_id} was not found.")
    return tool


@router.get("/lifecycle")
def describe_lifecycle(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The whole state machine, so a client need not hard-code it."""
    ensure_agent_can(session, agent_auth, READ)
    return envelope(lifecycle.describe())


@router.post("/tools", status_code=201)
def register_tool(
    body: RegisterRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = service.register(
        session,
        org_id=agent_auth.org.id,
        tool_key=body.tool_key,
        name=body.name,
        summary=body.summary,
        description=body.description,
        category=body.category,
        tags=body.tags,
        regions=body.regions,
        compliance=body.compliance,
        project_id=body.project_id,
        integration_id=body.integration_id,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="registry.tool_registered",
        summary=f"Tool '{tool.tool_key}' registered",
        target_type="registry_tool",
        target_id=str(tool.id),
        metadata={"tool_key": tool.tool_key, "category": tool.category},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(tool)
    return envelope(service.serialize(session, tool, detail=True))


@router.get("/tools")
def list_tools(
    lifecycle_state: str | None = Query(default=None),
    visibility: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    tools = service.list_tools(
        session,
        org_id=agent_auth.org.id,
        lifecycle_state=lifecycle_state,
        visibility=visibility,
        limit=limit,
        offset=offset,
    )
    return envelope({"tools": [service.serialize(session, tool) for tool in tools]})


@router.get("/tools/{tool_id}")
def get_tool(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    return envelope(
        service.serialize(session, _load(session, tool_id, agent_auth.org.id), detail=True)
    )


@router.patch("/tools/{tool_id}")
def update_tool(
    tool_id: uuid.UUID,
    body: UpdateRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    changed = service.update(session, tool, body.model_dump(exclude_none=True))
    if changed:
        record_audit(
            session,
            org_id=agent_auth.org.id,
            action="registry.tool_updated",
            summary=f"Tool '{tool.tool_key}' updated ({', '.join(changed)})",
            target_type="registry_tool",
            target_id=str(tool.id),
            metadata={"changed": changed},
            **actor_from_agent_auth(agent_auth),
        )
    session.commit()
    session.refresh(tool)
    return envelope({**service.serialize(session, tool, detail=True), "changed": changed})


@router.get("/tools/{tool_id}/trust")
def get_trust(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The score with every component's evidence, including what was not measured."""
    ensure_agent_can(session, agent_auth, READ)
    tool = _load(session, tool_id, agent_auth.org.id)
    return envelope(trust.compute(session, tool).as_dict())


# ── Versions ─────────────────────────────────────────────────────────────────


@router.post("/tools/{tool_id}/versions", status_code=201)
def create_version(
    tool_id: uuid.UUID,
    body: VersionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Cut the next immutable version, normally from a validated artifact."""
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    artifact = None
    if body.artifact_id is not None:
        artifact = artifacts.get(session, body.artifact_id, agent_auth.org.id)
        if artifact is None:
            raise NotFoundError(f"Runtime artifact {body.artifact_id} was not found.")
    version = versions.create(
        session,
        tool=tool,
        artifact=artifact,
        notes=body.notes,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    registry_events.version_created(
        session,
        org_id=tool.org_id,
        tool_id=tool.id,
        version=version.version,
        build_hash=version.build_hash,
        artifact_id=version.artifact_id,
        tool_count=version.tool_count,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="registry.version_created",
        summary=f"Tool '{tool.tool_key}' version {version.version} created",
        target_type="registry_tool",
        target_id=str(tool.id),
        metadata={"version": version.version, "build_hash": version.build_hash},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(version)
    return envelope(versions.serialize(version, detail=True))


@router.get("/tools/{tool_id}/versions")
def list_versions(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    tool = _load(session, tool_id, agent_auth.org.id)
    return envelope(
        {"versions": [versions.serialize(v) for v in versions.list_for_tool(session, tool.id)]}
    )


@router.get("/tools/{tool_id}/versions/{version}")
def get_version(
    tool_id: uuid.UUID,
    version: int,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    tool = _load(session, tool_id, agent_auth.org.id)
    row = versions.get(session, tool.id, version)
    if row is None:
        raise NotFoundError(f"Version {version} of '{tool.tool_key}' was not found.")
    return envelope(versions.serialize(row, detail=True))


# ── Lifecycle, visibility, pricing ───────────────────────────────────────────


@router.post("/tools/{tool_id}/transition")
def transition_tool(
    tool_id: uuid.UUID,
    body: TransitionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Move along the lifecycle. Gated transitions return a change request."""
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    try:
        outcome = service.transition(
            session,
            tool,
            body.to,
            reason=body.reason,
            requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
        )
    except lifecycle.TransitionError as exc:
        raise InvalidRequestError(str(exc), code="invalid_transition")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="registry.transition_requested" if not outcome.applied else "registry.transitioned",
        summary=(
            f"Tool '{tool.tool_key}' {'moved to' if outcome.applied else 'requested move to'} "
            f"{body.to}"
        ),
        target_type="registry_tool",
        target_id=str(tool.id),
        metadata={"to": body.to, "applied": outcome.applied},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(tool)
    return envelope({**outcome.as_dict(), "tool": service.serialize(session, tool)})


@router.post("/tools/{tool_id}/visibility", status_code=201)
def request_visibility(
    tool_id: uuid.UUID,
    body: VisibilityRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Ask to change who can see the tool. Governance decides (LLD §3.7)."""
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    request = service.request_visibility(
        session,
        tool,
        visibility=body.visibility,
        reason=body.reason,
        requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.commit()
    session.refresh(request)
    return envelope(service.serialize_change_request(request))


@router.post("/tools/{tool_id}/pricing", status_code=201)
def request_pricing(
    tool_id: uuid.UUID,
    body: PricingRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Ask to change the price. Governance decides (LLD §3.7)."""
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    request = service.request_pricing(
        session,
        tool,
        price=body.model_dump(exclude={"reason"}),
        reason=body.reason,
        requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.commit()
    session.refresh(request)
    return envelope(service.serialize_change_request(request))


@router.get("/tools/{tool_id}/pricing")
def get_pricing(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The live price and every price it has had."""
    ensure_agent_can(session, agent_auth, READ)
    tool = _load(session, tool_id, agent_auth.org.id)
    return envelope(
        {
            "current": pricing.serialize(pricing.current(session, tool.id)),
            "history": [pricing.serialize(row) for row in pricing.history(session, tool.id)],
        }
    )


@router.post("/tools/{tool_id}/deprecate")
def deprecate_tool(
    tool_id: uuid.UUID,
    body: DeprecateRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    try:
        outcome = service.deprecate(
            session,
            tool,
            note=body.note,
            requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
        )
    except lifecycle.TransitionError as exc:
        raise InvalidRequestError(str(exc), code="invalid_transition")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="registry.tool_deprecated",
        summary=f"Tool '{tool.tool_key}' deprecated",
        target_type="registry_tool",
        target_id=str(tool.id),
        metadata={"note": body.note[:200]},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(tool)
    return envelope({**outcome.as_dict(), "tool": service.serialize(session, tool)})


@router.post("/tools/{tool_id}/archive")
def archive_tool(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = _load(session, tool_id, agent_auth.org.id)
    try:
        outcome = service.archive(
            session, tool, requested_by_user_id=agent_auth.user.id if agent_auth.user else None
        )
    except lifecycle.TransitionError as exc:
        raise InvalidRequestError(str(exc), code="invalid_transition")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="registry.tool_archived",
        summary=f"Tool '{tool.tool_key}' archived",
        target_type="registry_tool",
        target_id=str(tool.id),
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(tool)
    return envelope({**outcome.as_dict(), "tool": service.serialize(session, tool)})


# ── Change requests ──────────────────────────────────────────────────────────


@router.get("/change-requests")
def list_change_requests(
    tool_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = service.list_change_requests(
        session, org_id=agent_auth.org.id, tool_id=tool_id, status=status
    )
    return envelope({"change_requests": [service.serialize_change_request(r) for r in rows]})


@router.post("/change-requests/{request_id}/decide")
def decide_change_request(
    request_id: uuid.UUID,
    body: DecisionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Approve or reject. An approval applies the change in the same transaction."""
    ensure_agent_can(session, agent_auth, DECIDE)
    request = service.get_change_request(session, request_id, agent_auth.org.id)
    if request is None:
        raise NotFoundError(f"Change request {request_id} was not found.")
    decided = service.decide(
        session,
        request,
        approve=body.approve,
        decided_by_user_id=agent_auth.user.id if agent_auth.user else None,
        note=body.note,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="registry.change_decided",
        summary=f"{decided.kind} change {decided.status}",
        target_type="registry_tool",
        target_id=str(decided.tool_id),
        metadata={"kind": decided.kind, "status": decided.status},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(decided)
    return envelope(service.serialize_change_request(decided))


@router.post("/change-requests/{request_id}/withdraw")
def withdraw_change_request(
    request_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    request = service.get_change_request(session, request_id, agent_auth.org.id)
    if request is None:
        raise NotFoundError(f"Change request {request_id} was not found.")
    if request.status != STATUS_PENDING:
        raise ConflictError(f"This request was already {request.status}.")
    withdrawn = service.withdraw(
        session, request, user_id=agent_auth.user.id if agent_auth.user else None
    )
    session.commit()
    session.refresh(withdrawn)
    return envelope(service.serialize_change_request(withdrawn))
