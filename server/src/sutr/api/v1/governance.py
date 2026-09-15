"""Governance: policies, compliance, risk, reviews and exceptions.

LLD §5.2.13 names the surface: `POST /v1/governance/policies`, `/evaluate`,
`/exceptions`, and `GET /v1/governance/...`. All three are here, plus the
compliance, risk and review endpoints the engines behind them need.

`POST /evaluate` is the LLD's policy-evaluation entry point and it is
deliberately **read-only**: the decision point is separate from the enforcement
point (§5.2.4), and this route is what that separation is for — asking what
would happen without anything happening.

Everything is scoped to the caller's organization. Another tenant's policy, run
or exception reads as 404.
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
from sutr.governance import compliance, engine, exceptions, policies, review, risk
from sutr.models.registry_tool import RegistryTool
from sutr.provisioning import identity, pdp
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/governance", tags=["governance"])

# Authoring and deciding governance is the same class of act as writing a tool
# policy, which is what `policies:write` already means.
MANAGE = "policies:write"
DECIDE = "approvals:decide"
READ = "logs:read"


class PolicyRequest(BaseModel):
    key: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    kind: str = "access"
    description: str = ""
    document: dict = {}
    notes: str = ""


class DraftRequest(BaseModel):
    document: dict
    notes: str = ""


class TransitionRequest(BaseModel):
    to: str
    note: str = ""


class RollbackRequest(BaseModel):
    reason: str = ""


class EvaluateRequest(BaseModel):
    """The LLD's `/evaluate`. Read-only: it decides nothing into existence."""

    integration_id: str
    tool_name: str
    action: str = "invoke"
    permission: str | None = "tools:execute"
    as_agent_id: uuid.UUID | None = None


class ComplianceRequest(BaseModel):
    framework: str
    target_type: str = "org"
    target_id: str = ""
    # Exercises §5.2.11's timeout path. Named for what it is rather than
    # hidden, because a fail-safe nobody can trigger is one nobody has seen.
    simulate_timeout: str = ""


class ReviewRequest(BaseModel):
    tool_id: uuid.UUID


class ReviewDecision(BaseModel):
    approve: bool
    note: str = ""


class ExceptionRequest(BaseModel):
    violation: str = Field(min_length=1, max_length=500)
    justification: str = Field(min_length=1, max_length=2000)
    control: str = ""
    policy_id: uuid.UUID | None = None
    scope_type: str = "registry_tool"
    scope_id: str = ""
    days: int = Field(default=exceptions.DEFAULT_DAYS, ge=1, le=exceptions.MAX_DAYS)


class ExceptionAssessment(BaseModel):
    assessment: dict


class ExceptionDecision(BaseModel):
    approve: bool
    note: str = ""


class RevalidateRequest(BaseModel):
    days: int = Field(default=exceptions.DEFAULT_DAYS, ge=1, le=exceptions.MAX_DAYS)
    note: str = ""


def _load_policy(session: Session, policy_id: uuid.UUID, org_id: uuid.UUID):
    policy = policies.get(session, policy_id, org_id)
    if policy is None:
        raise NotFoundError(f"Policy {policy_id} was not found.")
    return policy


@router.get("/capabilities")
def capabilities(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Which governance points are gated, and what each engine can honestly do."""
    ensure_agent_can(session, agent_auth, READ)
    return envelope(engine.describe())


# ── Policies ─────────────────────────────────────────────────────────────────


@router.post("/policies", status_code=201)
def create_policy(
    body: PolicyRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    policy, version = policies.create(
        session,
        org_id=agent_auth.org.id,
        key=body.key,
        name=body.name,
        kind=body.kind,
        description=body.description,
        document=body.document,
        notes=body.notes,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="governance.policy_created",
        summary=f"Policy '{policy.key}' created",
        target_type="governance_policy",
        target_id=str(policy.id),
        metadata={"kind": policy.kind},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(policy)
    session.refresh(version)
    return envelope({**policies.serialize(policy), "version": policies.serialize_version(version)})


@router.get("/policies")
def list_policies(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = policies.list_policies(session, org_id=agent_auth.org.id)
    return envelope(
        {"policies": [policies.serialize(policy) for policy in rows], **policies.describe()}
    )


@router.get("/policies/{policy_id}")
def get_policy(
    policy_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    policy = _load_policy(session, policy_id, agent_auth.org.id)
    return envelope(
        {
            **policies.serialize(policy),
            "versions": [
                policies.serialize_version(version)
                for version in policies.versions_for(session, policy.id)
            ],
        }
    )


@router.post("/policies/{policy_id}/versions", status_code=201)
def draft_version(
    policy_id: uuid.UUID,
    body: DraftRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    policy = _load_policy(session, policy_id, agent_auth.org.id)
    version = policies.draft(
        session,
        policy,
        document=body.document,
        notes=body.notes,
        authored_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.commit()
    session.refresh(version)
    return envelope(policies.serialize_version(version))


@router.post("/policies/{policy_id}/versions/{version}/transition")
def transition_version(
    policy_id: uuid.UUID,
    version: int,
    body: TransitionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Move a version along the lifecycle. Approval enforces separation of duties."""
    ensure_agent_can(session, agent_auth, DECIDE)
    policy = _load_policy(session, policy_id, agent_auth.org.id)
    row = policies.get_version(session, policy.id, version)
    if row is None:
        raise NotFoundError(f"Version {version} was not found.")
    actor = agent_auth.user.id if agent_auth.user else None
    try:
        if body.to == "ACTIVE":
            policies.activate(session, policy, row, actor_user_id=actor)
        else:
            policies.transition(session, policy, row, body.to, actor_user_id=actor, note=body.note)
    except policies.LifecycleError as exc:
        raise InvalidRequestError(str(exc), code="invalid_transition")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="governance.policy_transitioned",
        summary=f"Policy '{policy.key}' v{version} → {body.to}",
        target_type="governance_policy",
        target_id=str(policy.id),
        metadata={"version": version, "to": body.to},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(row)
    session.refresh(policy)
    return envelope(
        {"policy": policies.serialize(policy), "version": policies.serialize_version(row)}
    )


@router.post("/policies/{policy_id}/rollback")
def rollback_policy(
    policy_id: uuid.UUID,
    body: RollbackRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """§5.2.11: restore the last active version after a bad deployment."""
    ensure_agent_can(session, agent_auth, DECIDE)
    policy = _load_policy(session, policy_id, agent_auth.org.id)
    restored = policies.rollback(session, policy, reason=body.reason)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="governance.policy_rolled_back",
        summary=f"Policy '{policy.key}' rolled back to v{restored}",
        target_type="governance_policy",
        target_id=str(policy.id),
        metadata={"restored_version": restored, "reason": body.reason[:200]},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(policy)
    return envelope({**policies.serialize(policy), "restored_version": restored})


@router.post("/evaluate")
def evaluate(
    body: EvaluateRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What the policy decision point concludes. Changes nothing (§5.2.4)."""
    ensure_agent_can(session, agent_auth, READ)
    if body.as_agent_id is None:
        principal = identity.for_agent_auth(session, agent_auth)
    else:
        agent = identity.get(session, body.as_agent_id, agent_auth.org.id)
        if agent is None:
            raise NotFoundError(f"Agent {body.as_agent_id} was not found.")
        import json

        principal = identity.Principal(
            urn=identity.urn(identity.KIND_AGENT, agent.id),
            kind=identity.KIND_AGENT,
            org_id=agent.org_id,
            display=agent.name,
            agent=agent,
            attributes=json.loads(agent.attributes_json or "{}"),
        )
    decision = pdp.authorize(
        session,
        principal=principal,
        org_id=agent_auth.org.id,
        resource={"integration_id": body.integration_id, "tool_name": body.tool_name},
        action=body.action,
        permission=body.permission,
    )
    return envelope({**decision.as_dict(), "evaluated_as": principal.as_dict()})


# ── Compliance and risk ──────────────────────────────────────────────────────


@router.post("/compliance/runs", status_code=201)
def run_compliance(
    body: ComplianceRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    record = compliance.run(
        session,
        org_id=agent_auth.org.id,
        framework=body.framework,
        target_type=body.target_type,
        target_id=body.target_id,
        requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
        fail_with=body.simulate_timeout,
    )
    session.commit()
    session.refresh(record)
    return envelope(compliance.serialize(record, detail=True))


@router.get("/compliance/runs")
def list_compliance_runs(
    framework: str | None = Query(default=None),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    from sqlmodel import col, desc, select

    from sutr.models.governance_run import ComplianceRun

    ensure_agent_can(session, agent_auth, READ)
    statement = select(ComplianceRun).where(ComplianceRun.org_id == agent_auth.org.id)
    if framework:
        statement = statement.where(ComplianceRun.framework == framework)
    rows = session.exec(statement.order_by(desc(col(ComplianceRun.started_at))).limit(50)).all()
    return envelope(
        {
            "runs": [compliance.serialize(row) for row in rows],
            "frameworks": compliance.describe()["frameworks"],
            "certifies": False,
        }
    )


@router.get("/compliance/runs/{run_id}")
def get_compliance_run(
    run_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    from sutr.models.governance_run import ComplianceRun

    ensure_agent_can(session, agent_auth, READ)
    record = session.get(ComplianceRun, run_id)
    if record is None or record.org_id != agent_auth.org.id:
        raise NotFoundError(f"Compliance run {run_id} was not found.")
    return envelope(compliance.serialize(record, detail=True))


@router.get("/risk/{tool_id}")
def get_risk(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The risk score for one registry tool. **Lower is better.**"""
    ensure_agent_can(session, agent_auth, READ)
    tool = session.get(RegistryTool, tool_id)
    if tool is None or tool.org_id != agent_auth.org.id:
        raise NotFoundError(f"Registry tool {tool_id} was not found.")
    return envelope({**risk.compute(session, tool).as_dict(), "tool_id": str(tool.id)})


# ── The approval workflow ────────────────────────────────────────────────────


@router.post("/reviews", status_code=201)
def open_review(
    body: ReviewRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    tool = session.get(RegistryTool, body.tool_id)
    if tool is None or tool.org_id != agent_auth.org.id:
        raise NotFoundError(f"Registry tool {body.tool_id} was not found.")
    record = review.open_review(
        session,
        tool=tool,
        requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.commit()
    session.refresh(record)
    return envelope(review.serialize(record))


@router.get("/reviews")
def list_reviews(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = review.list_reviews(session, org_id=agent_auth.org.id)
    return envelope({"reviews": [review.serialize(row) for row in rows]})


def _load_review(session: Session, review_id: uuid.UUID, org_id: uuid.UUID):
    record = review.get(session, review_id, org_id)
    if record is None:
        raise NotFoundError(f"Review {review_id} was not found.")
    return record


@router.get("/reviews/{review_id}")
def get_review(
    review_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    return envelope(review.serialize(_load_review(session, review_id, agent_auth.org.id)))


@router.post("/reviews/{review_id}/refresh")
def refresh_review(
    review_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Re-read the evidence. The stages are sourced, so they can move on their own."""
    ensure_agent_can(session, agent_auth, MANAGE)
    record = _load_review(session, review_id, agent_auth.org.id)
    review.refresh(session, record, review.load_tool(session, record))
    session.commit()
    session.refresh(record)
    return envelope(review.serialize(record))


@router.post("/reviews/{review_id}/decide")
def decide_review(
    review_id: uuid.UUID,
    body: ReviewDecision,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, DECIDE)
    record = _load_review(session, review_id, agent_auth.org.id)
    review.decide(
        session,
        record,
        approve=body.approve,
        decided_by_user_id=agent_auth.user.id if agent_auth.user else None,
        note=body.note,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="governance.review_decided",
        summary=f"Review {record.state}",
        target_type="registry_tool",
        target_id=str(record.tool_id),
        metadata={"state": record.state, "risk_score": record.risk_score},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(record)
    return envelope(review.serialize(record))


@router.post("/reviews/{review_id}/auto-approve")
def auto_approve_review(
    review_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """§5.2.8's *"low-risk tools can auto-approve"*. Unknown risk is not low risk."""
    ensure_agent_can(session, agent_auth, DECIDE)
    record = _load_review(session, review_id, agent_auth.org.id)
    review.auto_approve(session, record)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="governance.review_auto_approved",
        summary=f"Review auto-approved at risk {record.risk_score}/100",
        target_type="registry_tool",
        target_id=str(record.tool_id),
        metadata={"risk_score": record.risk_score, "auto_approved": True},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(record)
    return envelope(review.serialize(record))


# ── Exceptions ───────────────────────────────────────────────────────────────


@router.post("/exceptions", status_code=201)
def request_exception(
    body: ExceptionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    record = exceptions.request(
        session,
        org_id=agent_auth.org.id,
        violation=body.violation,
        justification=body.justification,
        control=body.control,
        policy_id=body.policy_id,
        scope_type=body.scope_type,
        scope_id=body.scope_id,
        days=body.days,
        requested_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.commit()
    session.refresh(record)
    return envelope(exceptions.serialize(record))


@router.get("/exceptions")
def list_exceptions(
    state: str | None = Query(default=None),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = exceptions.list_exceptions(session, org_id=agent_auth.org.id, state=state)
    return envelope(
        {
            "exceptions": [exceptions.serialize(row) for row in rows],
            **exceptions.describe(),
        }
    )


def _load_exception(session: Session, exception_id: uuid.UUID, org_id: uuid.UUID):
    record = exceptions.get(session, exception_id, org_id)
    if record is None:
        raise NotFoundError(f"Exception {exception_id} was not found.")
    return record


@router.post("/exceptions/{exception_id}/assess")
def assess_exception(
    exception_id: uuid.UUID,
    body: ExceptionAssessment,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The risk assessment step, which approval requires."""
    ensure_agent_can(session, agent_auth, MANAGE)
    record = _load_exception(session, exception_id, agent_auth.org.id)
    exceptions.assess(session, record, assessment=body.assessment)
    session.commit()
    session.refresh(record)
    return envelope(exceptions.serialize(record))


@router.post("/exceptions/{exception_id}/decide")
def decide_exception(
    exception_id: uuid.UUID,
    body: ExceptionDecision,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, DECIDE)
    record = _load_exception(session, exception_id, agent_auth.org.id)
    exceptions.decide(
        session,
        record,
        approve=body.approve,
        decided_by_user_id=agent_auth.user.id if agent_auth.user else None,
        note=body.note,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="governance.exception_decided",
        summary=f"Exception {record.state} until {record.expires_at.isoformat()}",
        target_type="governance_exception",
        target_id=str(record.id),
        metadata={"state": record.state, "expires_at": record.expires_at.isoformat()},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(record)
    return envelope(exceptions.serialize(record))


@router.post("/exceptions/{exception_id}/revalidate")
def revalidate_exception(
    exception_id: uuid.UUID,
    body: RevalidateRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, DECIDE)
    record = _load_exception(session, exception_id, agent_auth.org.id)
    exceptions.revalidate(session, record, days=body.days, note=body.note)
    session.commit()
    session.refresh(record)
    return envelope(exceptions.serialize(record))


@router.post("/exceptions/{exception_id}/revoke")
def revoke_exception(
    exception_id: uuid.UUID,
    body: RollbackRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, DECIDE)
    record = _load_exception(session, exception_id, agent_auth.org.id)
    exceptions.revoke(session, record, reason=body.reason)
    session.commit()
    session.refresh(record)
    return envelope(exceptions.serialize(record))
