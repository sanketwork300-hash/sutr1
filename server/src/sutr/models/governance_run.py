import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# Compliance run states. `TIMED_OUT` is its own state because §5.2.11 gives it
# its own behaviour: *"Compliance scan timeout — mark pending; block
# publication."* Collapsing it into `FAILED` would lose the instruction.
STATE_RUNNING = "running"
STATE_COMPLETE = "complete"
STATE_TIMED_OUT = "timed_out"
STATE_FAILED = "failed"

# Control outcomes. `MANUAL` is the load-bearing one: a control this platform
# cannot check from what it holds is *not* a pass.
RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_MANUAL = "manual"
RESULT_NOT_APPLICABLE = "not_applicable"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ComplianceRun(SQLModel, table=True):
    """One evaluation of one framework against one target (ESDS LLD §5.2.6).

    *"results become tool governance metadata"* — so a run is kept rather than
    reduced to a boolean. The row holds every control's outcome and the
    evidence behind it, because a compliance claim nobody can trace is a
    compliance claim nobody should act on.
    """

    __tablename__ = "compliance_run"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    framework: str = Field(index=True)
    # What was assessed: "org" for a tenant-wide run, "registry_tool" for one
    # tool. A string pair rather than a foreign key, because the target kinds
    # live in different services and a column per kind would be a migration
    # every time a new one is assessed.
    target_type: str = Field(default="org", index=True)
    target_id: str = Field(default="", index=True)

    state: str = Field(default=STATE_RUNNING, index=True)
    results_json: str = Field(default="[]")
    # Counts, so a listing does not have to parse every result.
    passed: int = Field(default=0)
    failed: int = Field(default=0)
    manual: int = Field(default=0)
    not_applicable: int = Field(default=0)
    failure_reason: str = ""

    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    requested_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")


class RiskAssessment(SQLModel, table=True):
    """A risk score for one target (ESDS LLD §5.2.7).

    *"dynamic score per provider/tool (lower = better, e.g. 18/100)"*. The
    direction matters and is the opposite of the registry's trust score, so the
    two are never accidentally compared: **lower is better here**.

    `stale` is what §5.2.11's *"Risk calc failure — keep previous score, flag
    recalc"* looks like in a schema. A failed recalculation does not overwrite
    a good score with a bad one or with nothing; it marks the existing row and
    says why.
    """

    __tablename__ = "risk_assessment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    target_type: str = Field(default="registry_tool", index=True)
    target_id: str = Field(default="", index=True)

    # 0–100, lower is better. Null when nothing could be measured — never 0,
    # which would read as "no risk at all".
    score: int | None = None
    components_json: str = Field(default="[]")
    coverage: float = Field(default=0.0)
    unavailable_reason: str = ""

    stale: bool = Field(default=False, index=True)
    stale_reason: str = ""
    computed_at: datetime = Field(default_factory=_utcnow)
