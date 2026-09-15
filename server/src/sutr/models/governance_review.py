import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

# The LLD's approval workflow (§5.2.8), in order:
#     Upload → Automated Validation → Security Scan → Compliance Review →
#     Manual Approval → Publication
STAGE_UPLOAD = "upload"
STAGE_VALIDATION = "automated_validation"
STAGE_SCAN = "security_scan"
STAGE_COMPLIANCE = "compliance_review"
STAGE_APPROVAL = "manual_approval"
STAGE_PUBLICATION = "publication"

STAGES = (
    STAGE_UPLOAD,
    STAGE_VALIDATION,
    STAGE_SCAN,
    STAGE_COMPLIANCE,
    STAGE_APPROVAL,
    STAGE_PUBLICATION,
)

STATE_OPEN = "open"
STATE_APPROVED = "approved"
STATE_REJECTED = "rejected"
STATE_BLOCKED = "blocked"
STATE_WITHDRAWN = "withdrawn"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GovernanceReview(SQLModel, table=True):
    """One tool's passage through the approval workflow (ESDS LLD §5.2.8).

    Every stage's outcome is **sourced from something that actually happened**
    — the generation validation report, the artifact's scan results, a
    compliance run, a risk assessment — rather than from a checkbox somebody
    ticked. A workflow whose stages are self-asserted is a workflow that
    approves whatever it is told to.

    `auto_approved` records the LLD's *"low-risk tools can auto-approve"*. It
    is a fact about how the decision was reached, kept because "a human
    approved this" and "the risk score was under the threshold" are different
    assurances and an auditor needs to tell them apart.
    """

    __tablename__ = "governance_review"
    __table_args__ = (UniqueConstraint("tool_id", "opened_at", name="uq_governance_review_open"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    tool_id: uuid.UUID = Field(foreign_key="registry_tool.id", index=True)

    state: str = Field(default=STATE_OPEN, index=True)
    # [{"stage": "...", "status": "...", "detail": {...}, "source": "..."}]
    stages_json: str = Field(default="[]")
    # The stage the workflow is waiting on, or the one that blocked it.
    current_stage: str = Field(default=STAGE_UPLOAD, index=True)
    blocked_reason: str = ""

    risk_score: int | None = None
    auto_approved: bool = Field(default=False)
    decision_note: str = ""

    requested_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    decided_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    decided_at: datetime | None = None
    opened_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
