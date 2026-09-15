import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

# The LLD's policy lifecycle (§5.2), verbatim and in order:
#     Draft → Review → Approved → Published → Active → Deprecated → Archived
#
# `PUBLISHED` and `ACTIVE` are two states because they are two facts: a version
# can be released without being the one currently enforced. Exactly one version
# of a policy is ACTIVE at a time, which is what makes "roll back to the last
# active version" (§5.2.11) a thing the schema can express.
DRAFT = "DRAFT"
REVIEW = "REVIEW"
APPROVED = "APPROVED"
PUBLISHED = "PUBLISHED"
ACTIVE = "ACTIVE"
DEPRECATED = "DEPRECATED"
ARCHIVED = "ARCHIVED"

LIFECYCLE = (DRAFT, REVIEW, APPROVED, PUBLISHED, ACTIVE, DEPRECATED, ARCHIVED)

# What a policy governs. `access` versions carry ABAC rules the decision point
# reads; the others are declarative metadata a reviewer reads. Named rather
# than free text so a policy that nothing evaluates is visibly that.
KIND_ACCESS = "access"
KIND_COMPLIANCE = "compliance"
KIND_OPERATIONAL = "operational"
KINDS = (KIND_ACCESS, KIND_COMPLIANCE, KIND_OPERATIONAL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GovernancePolicy(SQLModel, table=True):
    """A named, versioned policy (ESDS LLD §5.2).

    *"Policies are declarative, versioned, and deployed independently."* The
    row is the identity; every statement it makes lives in a version, and
    versions are immutable once they leave DRAFT.

    Deployed independently is the reason `active_version` exists as a pointer
    rather than a flag on a version: activating is moving the pointer, and
    rolling back is moving it back — one write, no window in which two versions
    are both live.
    """

    __tablename__ = "governance_policy"
    __table_args__ = (UniqueConstraint("org_id", "key", name="uq_governance_policy_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # Stable identity, and what an evaluation result refers to.
    key: str = Field(index=True)
    name: str
    description: str = ""
    kind: str = Field(default=KIND_ACCESS, index=True)

    # Newest version written, and the one being enforced. They differ whenever
    # a draft exists, which is most of the time.
    current_version: int = Field(default=0)
    active_version: int | None = Field(default=None, index=True)
    # Where `active_version` was before the last activation. This is what
    # §5.2.11's "roll back to last active version" restores.
    previous_active_version: int | None = None

    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class GovernancePolicyVersion(SQLModel, table=True):
    """One immutable version of a policy.

    A version stops being editable the moment it leaves DRAFT, because
    everything downstream — a review decision, an activation, an audit record —
    refers to *what it said*. A version that could change after approval would
    make the approval a statement about nothing.

    `authored_by_user_id` and `approved_by_user_id` are both recorded and must
    differ (§5.2.10). Separation of duties is only checkable if both sides are
    kept.
    """

    __tablename__ = "governance_policy_version"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_governance_policy_version"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    policy_id: uuid.UUID = Field(foreign_key="governance_policy.id", index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    version: int

    state: str = Field(default=DRAFT, index=True)
    # The declarative statement. For an `access` policy: {"rules": [...]}.
    document_json: str = Field(default="{}")
    notes: str = ""
    # Why a review was refused, when one was.
    decision_note: str = ""

    authored_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    approved_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    approved_at: datetime | None = None
    published_at: datetime | None = None
    activated_at: datetime | None = None
    deprecated_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
