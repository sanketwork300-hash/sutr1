"""Who is acting: principal URNs and the agent identities behind them.

LLD §4.3: *"Every user, agent, microservice, and runtime has a unique
identity."* Three of those four already existed in some form and one did not,
so the work here is less about inventing identities than about giving them a
**single, comparable form**:

    sutr:user:<uuid>         a person
    sutr:agent:<uuid>        a registered agent identity
    sutr:service:<name>      a platform service acting on its own behalf
    sutr:runtime:<uuid>      a deployed generated MCP server
    sutr:api-key:<uuid>      a credential with no identity bound to it yet

The last one is the honest part. An API key that has not been bound to an agent
identity is still an actor, and pretending it is an agent would attribute its
calls to something that does not exist. It gets its own scheme so that
"unidentified caller" is legible in a log line rather than absent from one.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import ConflictError, InvalidRequestError, NotFoundError
from sutr.models.agent_identity import KINDS, AgentIdentity
from sutr.models.api_key import ApiKey

SCHEME = "sutr"
KIND_USER = "user"
KIND_AGENT = "agent"
KIND_SERVICE = "service"
KIND_RUNTIME = "runtime"
KIND_API_KEY = "api-key"

PRINCIPAL_KINDS = (KIND_USER, KIND_AGENT, KIND_SERVICE, KIND_RUNTIME, KIND_API_KEY)

# Names end up in a URN, in a log line and in an audit record.
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")

_PRINCIPAL = re.compile(rf"^{SCHEME}:(?P<kind>[a-z-]+):(?P<id>[^:\s]+)$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def urn(kind: str, identifier: Any) -> str:
    if kind not in PRINCIPAL_KINDS:
        raise InvalidRequestError(f"Unknown principal kind '{kind}'.")
    return f"{SCHEME}:{kind}:{identifier}"


def parse(principal: str) -> tuple[str, str]:
    """(kind, identifier), or a refusal. Never a partial parse."""
    match = _PRINCIPAL.match(principal or "")
    if not match or match.group("kind") not in PRINCIPAL_KINDS:
        raise InvalidRequestError(f"'{principal}' is not a principal URN.")
    return match.group("kind"), match.group("id")


@dataclass
class Principal:
    """The acting identity for one request, in a form every layer can read."""

    urn: str
    kind: str
    org_id: uuid.UUID
    display: str = ""
    agent: AgentIdentity | None = None
    role: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "principal": self.urn,
            "kind": self.kind,
            "org_id": str(self.org_id),
            "display": self.display,
            "role": self.role,
            "attributes": self.attributes,
            # The tenant's own claims about their own agent. Nothing here has
            # verified them, and a reader deciding on them should know that.
            "attributes_are_tenant_declared": True,
        }


def for_agent_auth(session: Session, auth) -> Principal:
    """The principal behind an authenticated request.

    A user acting directly is a user. An API key bound to an agent identity is
    that agent, with its attributes and its tenant's role for the key's
    creator. An unbound key is an `api-key` principal — an actor with no
    identity, said plainly rather than dressed as one.
    """
    from sutr.models.org_membership import OrgMembership

    org_id = auth.org.id
    if auth.api_key is not None:
        agent = bound_agent(session, auth.api_key.id)
        if agent is not None:
            return Principal(
                urn=urn(KIND_AGENT, agent.id),
                kind=KIND_AGENT,
                org_id=org_id,
                display=agent.name,
                agent=agent,
                attributes=json.loads(agent.attributes_json or "{}"),
            )
        return Principal(
            urn=urn(KIND_API_KEY, auth.api_key.id),
            kind=KIND_API_KEY,
            org_id=org_id,
            display=auth.api_key.name,
        )

    if auth.user is not None:
        membership = session.exec(
            select(OrgMembership)
            .where(OrgMembership.user_id == auth.user.id)
            .where(OrgMembership.org_id == org_id)
        ).first()
        return Principal(
            urn=urn(KIND_USER, auth.user.id),
            kind=KIND_USER,
            org_id=org_id,
            display=auth.user.email,
            role=membership.role if membership else None,
        )

    # Neither a user nor a key: the caller is the platform itself.
    return Principal(urn=urn(KIND_SERVICE, "platform"), kind=KIND_SERVICE, org_id=org_id)


def bound_agent(session: Session, api_key_id: uuid.UUID) -> AgentIdentity | None:
    return session.exec(
        select(AgentIdentity)
        .where(AgentIdentity.api_key_id == api_key_id)
        .where(AgentIdentity.active == True)  # noqa: E712
    ).first()


# ── The registry of agent identities ─────────────────────────────────────────


def get(session: Session, agent_id: uuid.UUID, org_id: uuid.UUID) -> AgentIdentity | None:
    agent = session.get(AgentIdentity, agent_id)
    if agent is None or agent.org_id != org_id:
        # One answer for "no such agent" and "another tenant's agent".
        return None
    return agent


def list_agents(session: Session, *, org_id: uuid.UUID) -> list[AgentIdentity]:
    return list(
        session.exec(
            select(AgentIdentity)
            .where(AgentIdentity.org_id == org_id)
            .order_by(desc(col(AgentIdentity.created_at)))
        ).all()
    )


def register(
    session: Session,
    *,
    org_id: uuid.UUID,
    name: str,
    description: str = "",
    kind: str = KIND_AGENT,
    attributes: dict[str, Any] | None = None,
    api_key_id: uuid.UUID | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> AgentIdentity:
    """Create an identity. The caller commits."""
    if not NAME.match(name or ""):
        raise InvalidRequestError(
            "An agent name is 3–64 characters of lowercase letters, digits, hyphens and "
            "underscores. It appears in principal URNs, logs and audit records."
        )
    if kind not in KINDS:
        raise InvalidRequestError(f"Unknown agent kind '{kind}'. Known: {', '.join(KINDS)}.")
    existing = session.exec(
        select(AgentIdentity)
        .where(AgentIdentity.org_id == org_id)
        .where(AgentIdentity.name == name)
    ).first()
    if existing is not None:
        raise ConflictError(f"An agent called '{name}' already exists in this organization.")
    if api_key_id is not None:
        _check_key(session, api_key_id, org_id)

    agent = AgentIdentity(
        org_id=org_id,
        name=name,
        description=description,
        kind=kind,
        api_key_id=api_key_id,
        attributes_json=json.dumps(_normalize(attributes or {}), sort_keys=True),
        created_by_user_id=created_by_user_id,
    )
    session.add(agent)
    session.flush()
    return agent


def _check_key(session: Session, api_key_id: uuid.UUID, org_id: uuid.UUID) -> ApiKey:
    key = session.get(ApiKey, api_key_id)
    if key is None or key.org_id != org_id:
        raise NotFoundError("That API key was not found.")
    bound = session.exec(
        select(AgentIdentity)
        .where(AgentIdentity.api_key_id == api_key_id)
        .where(AgentIdentity.active == True)  # noqa: E712
    ).first()
    if bound is not None:
        raise ConflictError(
            f"That API key is already bound to the agent '{bound.name}'. A credential "
            "authenticates one identity; binding it to two would make every call ambiguous."
        )
    return key


def _normalize(attributes: dict[str, Any]) -> dict[str, str]:
    """Attributes are compared, so they are stored comparably.

    Lowercased strings throughout. An attribute that is `"India"` in a rule and
    `"india"` on an identity is a rule that silently never matches, which is
    the worst failure an authorization layer can have.
    """
    return {
        str(key).strip().lower(): str(value).strip().lower() for key, value in attributes.items()
    }


def update(session: Session, agent: AgentIdentity, changes: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for field_name in ("description",):
        if field_name in changes and changes[field_name] != getattr(agent, field_name):
            setattr(agent, field_name, changes[field_name])
            changed.append(field_name)
    if "attributes" in changes:
        value = json.dumps(_normalize(changes["attributes"] or {}), sort_keys=True)
        if value != agent.attributes_json:
            agent.attributes_json = value
            changed.append("attributes")
    if "api_key_id" in changes:
        key_id = changes["api_key_id"]
        if key_id is not None:
            _check_key(session, key_id, agent.org_id)
        if key_id != agent.api_key_id:
            agent.api_key_id = key_id
            changed.append("api_key_id")
    if changed:
        agent.updated_at = _utcnow()
        session.add(agent)
    return changed


def revoke(session: Session, agent: AgentIdentity, *, reason: str = "") -> AgentIdentity:
    """Retire an identity.

    Deactivated rather than deleted: the passes it was issued and the calls it
    made refer to it, and an identity that vanishes turns its own history into
    a set of unattributable rows.
    """
    agent.active = False
    agent.revoked_at = _utcnow()
    agent.revoked_reason = reason
    agent.updated_at = agent.revoked_at
    session.add(agent)
    return agent


def touch(session: Session, agent: AgentIdentity) -> None:
    agent.last_seen_at = _utcnow()
    session.add(agent)


def serialize(agent: AgentIdentity) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "principal": urn(KIND_AGENT, agent.id),
        "name": agent.name,
        "description": agent.description,
        "kind": agent.kind,
        "api_key_id": str(agent.api_key_id) if agent.api_key_id else None,
        "attributes": json.loads(agent.attributes_json or "{}"),
        "attributes_are_tenant_declared": True,
        "active": agent.active,
        "revoked_at": agent.revoked_at.isoformat() if agent.revoked_at else None,
        "revoked_reason": agent.revoked_reason or None,
        "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        "created_at": agent.created_at.isoformat(),
    }
