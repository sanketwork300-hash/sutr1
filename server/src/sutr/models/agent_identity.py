import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

# What kind of thing holds this identity. LLD §4.3: *"Every user, agent,
# microservice, and runtime has a unique identity"*. Users already have one
# (`user`), and runtimes derive theirs from their deployment id, so this table
# holds the two that had none.
KIND_AGENT = "agent"
KIND_SERVICE = "service"
KINDS = (KIND_AGENT, KIND_SERVICE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentIdentity(SQLModel, table=True):
    """A named, attributable actor inside one tenant (ESDS LLD §4.3.1).

    Before this, an agent calling the platform was an API key: a credential
    with a label and no attributes, no lifecycle of its own, and no way to say
    *what kind of agent* it is. That makes least privilege impossible to
    express — you cannot scope a pass to "the finance agent" if there is no
    finance agent, only a key somebody named "finance".

    So an identity is separate from the credential that authenticates it. A key
    may be bound to one (`api_key_id`), and the identity outlives key rotation:
    revoking a leaked key does not revoke the agent's history, its attributes,
    or the passes issued to it.

    `attributes_json` carries the subject attributes the ABAC layer reads —
    department, region, clearance, whatever the tenant decides. They are the
    tenant's own claims about their own agent; nothing here verifies them, and
    that is stated wherever they are returned.
    """

    __tablename__ = "agent_identity"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_agent_identity_name"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    name: str = Field(index=True)
    description: str = ""
    kind: str = Field(default=KIND_AGENT, index=True)

    # The credential this identity authenticates with, when one is bound.
    # Optional: an identity can exist before its key, and survive after it.
    api_key_id: uuid.UUID | None = Field(default=None, foreign_key="api_key.id", index=True)

    # Subject attributes for the ABAC layer. The tenant's claims about their
    # own agent; nothing verifies them.
    attributes_json: str = Field(default="{}")

    active: bool = Field(default=True, index=True)
    revoked_at: datetime | None = None
    revoked_reason: str = ""

    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime | None = None
