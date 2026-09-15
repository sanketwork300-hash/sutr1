import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# The LLD's exception flow (§5.2.9):
#     Violation → Request → Risk Assessment → Decision → Temporary exception →
#     Expiry → Revalidation
STATE_REQUESTED = "requested"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"
STATE_REVOKED = "revoked"

STATES = (STATE_REQUESTED, STATE_APPROVED, STATE_REJECTED, STATE_EXPIRED, STATE_REVOKED)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GovernanceException(SQLModel, table=True):
    """A time-boxed exemption from a policy (ESDS LLD §5.2.9).

    *"All exceptions expire."* `expires_at` is therefore **not nullable** and
    has no "never" value: a permanent exception is not an exception, it is a
    policy change nobody wrote down. The service caps how far ahead it may be
    set, so "expires" cannot be satisfied by a date in 2099.

    `revalidate_at` is the LLD's revalidation step: an approved exception names
    when somebody must look at it again, which is earlier than expiry and is
    what stops a 90-day exemption being reviewed on day 89.
    """

    __tablename__ = "governance_exception"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    # What is being excepted from, and for what. The policy is optional
    # because an exception can be raised against a control or a scan finding
    # that no policy row owns.
    policy_id: uuid.UUID | None = Field(default=None, foreign_key="governance_policy.id")
    control: str = Field(default="", index=True)
    scope_type: str = Field(default="registry_tool", index=True)
    scope_id: str = Field(default="", index=True)

    # The violation this exists because of, in the requester's words.
    violation: str = ""
    justification: str = ""

    state: str = Field(default=STATE_REQUESTED, index=True)
    # The risk assessment step, recorded so a decision can be explained.
    risk_json: str = Field(default="{}")
    decision_note: str = ""

    requested_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    decided_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    decided_at: datetime | None = None

    # Not nullable: all exceptions expire.
    expires_at: datetime = Field(index=True)
    revalidate_at: datetime | None = None
    revoked_reason: str = ""

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
