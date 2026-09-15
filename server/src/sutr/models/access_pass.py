import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccessPass(SQLModel, table=True):
    """The record of one issued scoped access pass (ESDS LLD §4.3).

    The LLD's five adjectives are the whole specification: *short-lived,
    single-purpose, least-privilege, signed, revocable — conceptually AWS STS
    temporary credentials. Issued by Provisioning after the policy decision.*

    **The token itself is not stored here.** This row is the claims, the
    decision that produced them, and the identifiers needed to revoke it. A
    table of live bearer tokens would be a table whose compromise is equivalent
    to compromising every agent at once; storing only what was granted means a
    database leak reveals what happened, not how to impersonate anybody.

    `decision_json` records *why* the pass was issued — which authorization
    layers ran and what they concluded. A credential whose issuance cannot be
    explained is a credential nobody can audit, and the LLD asks for passes to
    be issued "after the policy decision", which is only meaningful if the
    decision is kept.
    """

    __tablename__ = "access_pass"

    # The JWT's `jti`. Primary key rather than a separate id: the two would
    # always be one-to-one, and a second identifier is a second thing to get
    # out of step.
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    # Who it was issued to, as a principal URN (`sutr:agent:…`, `sutr:user:…`).
    principal: str = Field(index=True)
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agent_identity.id", index=True)
    issued_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")

    # Single-purpose: what this pass is for, and nothing else.
    purpose: str = ""
    audience: str = ""
    # The exact tools it covers. Never null in practice — a pass with no tool
    # list is a pass that covers everything, which is the opposite of least
    # privilege — but nullable so the shape can express it if it is ever
    # deliberately wanted.
    tools_json: str = Field(default="[]")
    # The integration or deployment the pass is scoped to.
    resource: str = ""

    nonce: str = Field(index=True)
    issued_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(index=True)
    # Set the first time the platform sees the pass presented. The generated
    # runtimes enforce single use themselves, in process; this is the
    # platform's own record and does not claim to be that enforcement.
    first_seen_at: datetime | None = None
    use_count: int = Field(default=0)

    revoked_at: datetime | None = Field(default=None, index=True)
    revoked_reason: str = ""

    decision_json: str = Field(default="{}")
