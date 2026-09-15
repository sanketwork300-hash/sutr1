"""Provisioning: identities, authorization decisions, and scoped access passes.

LLD §2.3 gives this service one job — *"Issue scoped access passes (temp
credentials)"* — and §4.3 says when: *"Issued by Provisioning after the policy
decision."*

The routes follow that order. `POST /decisions` asks the policy decision point
what it would conclude, and changes nothing; `POST /passes` asks the same
question and, if the answer is yes, mints a pass recording it. A pass without a
decision behind it is not issued, and the decision is stored with the pass so
the grant can be explained later.

Everything is scoped to the caller's organization. Another tenant's agent,
rule or pass reads as 404.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import InvalidRequestError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.provisioning import identity, passes, pdp, rules
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/provisioning", tags=["provisioning"])

# Creating identities and writing authorization rules is changing who may do
# what — the same class of act as writing a tool policy.
MANAGE = "policies:write"
# Asking for a pass is something an agent operator does routinely.
ISSUE = "integrations:manage"
READ = "logs:read"


class RegisterAgentRequest(BaseModel):
    name: str = Field(min_length=3, max_length=64)
    description: str = ""
    kind: str = "agent"
    attributes: dict[str, str] = {}
    api_key_id: uuid.UUID | None = None


class UpdateAgentRequest(BaseModel):
    description: str | None = None
    attributes: dict[str, str] | None = None
    api_key_id: uuid.UUID | None = None


class RevokeAgentRequest(BaseModel):
    reason: str = ""
    # Revoking the identity does not revoke what it already holds, unless you
    # say so — and in an incident you say so.
    revoke_passes: bool = True


class RuleRequest(BaseModel):
    name: str = Field(min_length=3, max_length=64)
    effect: str
    description: str = ""
    subject: dict[str, str] = {}
    resource: dict[str, str] = {}
    action: str = "invoke"
    priority: int = Field(default=100, ge=0, le=10_000)


class DecisionRequest(BaseModel):
    """Ask what the decision point would conclude. Changes nothing."""

    integration_id: str
    tool_name: str
    action: str = "invoke"
    permission: str | None = "tools:execute"
    # Evaluate as a specific agent rather than as the caller. For testing a
    # rule against the identity it was written for.
    as_agent_id: uuid.UUID | None = None


class PassRequest(BaseModel):
    tools: list[str] = Field(min_length=1, max_length=passes.MAX_TOOLS)
    integration_id: str
    purpose: str = Field(default="", max_length=200)
    resource: str = ""
    ttl_seconds: int = Field(
        default=passes.DEFAULT_TTL_SECONDS,
        ge=passes.MIN_TTL_SECONDS,
        le=passes.MAX_TTL_SECONDS,
    )
    as_agent_id: uuid.UUID | None = None


class RevokePassRequest(BaseModel):
    reason: str = ""


def _principal(session: Session, agent_auth: AgentAuth, as_agent_id: uuid.UUID | None):
    """The principal to evaluate: the caller, or an agent they own."""
    if as_agent_id is None:
        return identity.for_agent_auth(session, agent_auth)
    agent = identity.get(session, as_agent_id, agent_auth.org.id)
    if agent is None:
        raise NotFoundError(f"Agent {as_agent_id} was not found.")
    if not agent.active:
        raise InvalidRequestError(
            f"The agent '{agent.name}' is revoked, so nothing can be decided or issued for it."
        )
    import json

    return identity.Principal(
        urn=identity.urn(identity.KIND_AGENT, agent.id),
        kind=identity.KIND_AGENT,
        org_id=agent.org_id,
        display=agent.name,
        agent=agent,
        attributes=json.loads(agent.attributes_json or "{}"),
    )


@router.get("/capabilities")
def capabilities(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The authorization layers, the pass rules, and where secrets live."""
    from sutr import secrets

    ensure_agent_can(session, agent_auth, READ)
    return envelope(
        {
            "authorization": pdp.describe(),
            "access_passes": passes.describe(),
            "secrets": secrets.describe(),
            "principal_kinds": list(identity.PRINCIPAL_KINDS),
        }
    )


@router.get("/whoami")
def whoami(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The principal this request is acting as.

    Useful on its own, and the fastest way to see that an API key with no
    bound agent identity is an `api-key` principal rather than an agent.
    """
    ensure_agent_can(session, agent_auth, READ)
    return envelope(identity.for_agent_auth(session, agent_auth).as_dict())


# ── Agent identities ─────────────────────────────────────────────────────────


@router.post("/agents", status_code=201)
def register_agent(
    body: RegisterAgentRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    agent = identity.register(
        session,
        org_id=agent_auth.org.id,
        name=body.name,
        description=body.description,
        kind=body.kind,
        attributes=body.attributes,
        api_key_id=body.api_key_id,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="provisioning.agent_registered",
        summary=f"Agent identity '{agent.name}' registered",
        target_type="agent_identity",
        target_id=str(agent.id),
        metadata={"kind": agent.kind, "bound_key": bool(agent.api_key_id)},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(agent)
    return envelope(identity.serialize(agent))


@router.get("/agents")
def list_agents(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = identity.list_agents(session, org_id=agent_auth.org.id)
    return envelope({"agents": [identity.serialize(agent) for agent in rows]})


def _load_agent(session: Session, agent_id: uuid.UUID, org_id: uuid.UUID):
    agent = identity.get(session, agent_id, org_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id} was not found.")
    return agent


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    return envelope(identity.serialize(_load_agent(session, agent_id, agent_auth.org.id)))


@router.patch("/agents/{agent_id}")
def update_agent(
    agent_id: uuid.UUID,
    body: UpdateAgentRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    agent = _load_agent(session, agent_id, agent_auth.org.id)
    changed = identity.update(session, agent, body.model_dump(exclude_unset=True))
    session.commit()
    session.refresh(agent)
    return envelope({**identity.serialize(agent), "changed": changed})


@router.post("/agents/{agent_id}/revoke")
def revoke_agent(
    agent_id: uuid.UUID,
    body: RevokeAgentRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Retire an identity, and by default every live pass it holds.

    Revoking the identity alone would leave its outstanding passes valid until
    they expire, which is the wrong default for the situation this endpoint
    exists for.
    """
    ensure_agent_can(session, agent_auth, MANAGE)
    agent = _load_agent(session, agent_id, agent_auth.org.id)
    identity.revoke(session, agent, reason=body.reason)
    revoked = 0
    if body.revoke_passes:
        revoked = passes.revoke_for_principal(
            session,
            org_id=agent_auth.org.id,
            principal=identity.urn(identity.KIND_AGENT, agent.id),
            reason=body.reason or "the agent identity was revoked",
        )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="provisioning.agent_revoked",
        summary=f"Agent identity '{agent.name}' revoked",
        target_type="agent_identity",
        target_id=str(agent.id),
        metadata={"passes_revoked": revoked, "reason": body.reason[:200]},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(agent)
    return envelope({**identity.serialize(agent), "passes_revoked": revoked})


# ── Access rules ─────────────────────────────────────────────────────────────


@router.post("/rules", status_code=201)
def create_rule(
    body: RuleRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Write an ABAC rule. The LLD's example is the shape:

    `Finance role ∧ Organization=Bank-A ∧ Region=India ⇒ Allow Refund Tool`
    """
    ensure_agent_can(session, agent_auth, MANAGE)
    rule = rules.create(
        session,
        org_id=agent_auth.org.id,
        name=body.name,
        effect=body.effect,
        subject=body.subject,
        resource=body.resource,
        action=body.action,
        priority=body.priority,
        description=body.description,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="provisioning.rule_created",
        summary=f"Access rule '{rule.name}' ({rule.effect}) created",
        target_type="access_rule",
        target_id=str(rule.id),
        metadata={"effect": rule.effect, "action": rule.action},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(rule)
    return envelope(rules.serialize(rule))


@router.get("/rules")
def list_rules(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = rules.list_rules(session, org_id=agent_auth.org.id)
    return envelope(
        {
            "rules": [rules.serialize(rule) for rule in rows],
            "resource_attributes": list(rules.RESOURCE_KEYS),
            "actions": list(rules.ACTIONS),
            "evaluation": (
                "Deny wins. A rule set with no matching rule has no opinion, so a tenant with "
                "no rules keeps the behaviour they had before this layer existed."
            ),
        }
    )


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(
    rule_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    ensure_agent_can(session, agent_auth, MANAGE)
    rule = rules.get(session, rule_id, agent_auth.org.id)
    if rule is None:
        raise NotFoundError(f"Rule {rule_id} was not found.")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="provisioning.rule_deleted",
        summary=f"Access rule '{rule.name}' deleted",
        target_type="access_rule",
        target_id=str(rule.id),
        **actor_from_agent_auth(agent_auth),
    )
    rules.delete(session, rule)
    session.commit()


# ── Decisions and passes ─────────────────────────────────────────────────────


@router.post("/decisions")
def decide(
    body: DecisionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What the decision point concludes, and why. Changes nothing.

    The decision point is separate from the enforcement point (LLD §5.2), and
    this route is what that separation buys: a rule can be tested against the
    identity it was written for without invoking anything.
    """
    ensure_agent_can(session, agent_auth, READ)
    principal = _principal(session, agent_auth, body.as_agent_id)
    decision = pdp.authorize(
        session,
        principal=principal,
        org_id=agent_auth.org.id,
        resource={"integration_id": body.integration_id, "tool_name": body.tool_name},
        action=body.action,
        permission=body.permission,
    )
    return envelope({**decision.as_dict(), "evaluated_as": principal.as_dict()})


@router.post("/passes", status_code=201)
def issue_pass(
    body: PassRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Issue a scoped access pass — after the decision, never instead of it.

    The token comes back once and is not stored. What is stored is what was
    granted and the decision that granted it.
    """
    ensure_agent_can(session, agent_auth, ISSUE)
    principal = _principal(session, agent_auth, body.as_agent_id)

    granted: list[str] = []
    for tool in body.tools:
        decision = pdp.authorize(
            session,
            principal=principal,
            org_id=agent_auth.org.id,
            resource={"integration_id": body.integration_id, "tool_name": tool},
            action="invoke",
            permission="tools:execute",
        )
        if decision.allowed:
            granted.append(tool)
        else:
            last_refusal = decision
    # Least privilege: the pass covers what the decision allowed, which may be
    # less than was asked for. A refusal only when *nothing* was allowed.
    if not granted:
        raise InvalidRequestError(
            f"None of the requested tools are permitted: {last_refusal.denied_by}: "
            f"{last_refusal.reason}",
            code="authorization_denied",
            details={"decision": last_refusal.as_dict()},
        )

    overall = pdp.authorize(
        session,
        principal=principal,
        org_id=agent_auth.org.id,
        resource={"integration_id": body.integration_id, "tool_name": granted[0]},
        action="invoke",
        permission="tools:execute",
    )
    issued = passes.issue(
        session,
        org_id=agent_auth.org.id,
        principal=principal.urn,
        decision=overall,
        tools=body.tools,
        entitled_tools=granted,
        resource=body.resource or body.integration_id,
        purpose=body.purpose,
        ttl_seconds=body.ttl_seconds,
        agent_id=principal.agent.id if principal.agent else None,
        issued_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="provisioning.pass_issued",
        summary=f"Access pass issued to {principal.urn} for {len(issued.granted_tools)} tool(s)",
        target_type="access_pass",
        target_id=str(issued.record.id),
        metadata={
            "principal": principal.urn,
            "tools": issued.granted_tools,
            "ttl_seconds": body.ttl_seconds,
            "resource": issued.record.resource,
        },
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(issued.record)
    return envelope(issued.as_dict())


@router.get("/passes")
def list_passes(
    principal: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = passes.list_passes(session, org_id=agent_auth.org.id, principal=principal, limit=limit)
    return envelope({"passes": [passes.serialize(record) for record in rows]})


@router.get("/passes/{pass_id}")
def get_pass(
    pass_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    record = passes.get(session, pass_id, agent_auth.org.id)
    if record is None:
        raise NotFoundError(f"Access pass {pass_id} was not found.")
    return envelope(passes.serialize(record))


@router.post("/passes/{pass_id}/revoke")
def revoke_pass(
    pass_id: uuid.UUID,
    body: RevokePassRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, ISSUE)
    record = passes.get(session, pass_id, agent_auth.org.id)
    if record is None:
        raise NotFoundError(f"Access pass {pass_id} was not found.")
    passes.revoke(session, record, reason=body.reason)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="provisioning.pass_revoked",
        summary=f"Access pass revoked ({body.reason[:80] or 'no reason given'})",
        target_type="access_pass",
        target_id=str(record.id),
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(record)
    return envelope(
        {
            **passes.serialize(record),
            # The half-truth this endpoint must not tell.
            "note": (
                "Revocation is enforced where the platform verifies the pass. A generated MCP "
                "server validates offline and cannot see it, which is why passes are short-lived."
            ),
        }
    )
