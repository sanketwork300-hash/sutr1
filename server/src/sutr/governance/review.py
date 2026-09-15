"""The six-stage approval workflow (ESDS LLD §5.2.8).

    Upload → Automated Validation → Security Scan → Compliance Review →
    Manual Approval → Publication

with *"low-risk tools can auto-approve"*.

The design rule that makes this worth having: **every stage's outcome is
sourced from something that actually happened.** Upload reads whether the tool
has a registry version; validation reads the generation validation report;
security scan reads that report's scan checks; compliance reads a real
compliance run; approval reads a human decision or the risk score. A workflow
whose stages are ticked by whoever opened it approves whatever it is told to.

Stages that cannot be evidenced report `blocked` with the reason and the
workflow stops there — §5.2 again: *"Governance failures never silently permit
unauthorized actions — fail closed for high-risk operations."*
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import ConflictError, ForbiddenError, NotFoundError
from sutr.governance import compliance, risk
from sutr.models.governance_review import (
    STAGE_APPROVAL,
    STAGE_COMPLIANCE,
    STAGE_PUBLICATION,
    STAGE_SCAN,
    STAGE_UPLOAD,
    STAGE_VALIDATION,
    STAGES,
    STATE_APPROVED,
    STATE_BLOCKED,
    STATE_OPEN,
    STATE_REJECTED,
    STATE_WITHDRAWN,
    GovernanceReview,
)
from sutr.models.registry_tool import RegistryTool

PASSED = "passed"
BLOCKED = "blocked"
PENDING = "pending"

# Which framework the compliance stage runs. The organisation's own controls:
# every check the platform can actually make, with no external framework's
# scope implied.
REVIEW_FRAMEWORK = compliance.ORG_CONTROLS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StageResult:
    stage: str
    status: str
    detail: str = ""
    source: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "detail": self.detail,
            # Where the outcome came from. A stage with no source is a stage
            # somebody asserted.
            "source": self.source,
            "evidence": self.evidence,
        }


def _upload_stage(session: Session, tool: RegistryTool) -> StageResult:
    from sutr.registry import versions

    history = versions.list_for_tool(session, tool.id)
    if not history:
        return StageResult(
            stage=STAGE_UPLOAD,
            status=BLOCKED,
            detail="This tool has no version. There is nothing to review.",
            source="registry_version",
        )
    return StageResult(
        stage=STAGE_UPLOAD,
        status=PASSED,
        detail=f"{len(history)} version(s), newest v{history[0].version}.",
        source="registry_version",
        evidence={"versions": len(history), "newest": history[0].version},
    )


def _artifact_for(session: Session, tool: RegistryTool):
    from sutr.registry import versions

    for version in versions.list_for_tool(session, tool.id):
        if version.artifact_id is None:
            continue
        from sutr.models.runtime_artifact import RuntimeArtifact

        artifact = session.get(RuntimeArtifact, version.artifact_id)
        if artifact is not None:
            return artifact
    return None


def _validation_stage(session: Session, tool: RegistryTool) -> StageResult:
    from sutr.models.runtime_artifact import STATUS_VALIDATED

    artifact = _artifact_for(session, tool)
    if artifact is None:
        return StageResult(
            stage=STAGE_VALIDATION,
            status=BLOCKED,
            detail=(
                "No version of this tool was built from a runtime artifact, so there is no "
                "validation report to read."
            ),
            source="runtime_artifact",
        )
    report = json.loads(artifact.validation_json or "{}")
    if artifact.status != STATUS_VALIDATED:
        return StageResult(
            stage=STAGE_VALIDATION,
            status=BLOCKED,
            detail=(
                "The artifact was rejected: "
                + ", ".join(report.get("failed") or ["validation"])
                + "."
            ),
            source="runtime_artifact",
            evidence={"artifact_id": str(artifact.id), "failed": report.get("failed")},
        )
    return StageResult(
        stage=STAGE_VALIDATION,
        status=PASSED,
        detail=report.get("summary", "The artifact passed validation."),
        source="runtime_artifact",
        evidence={"artifact_id": str(artifact.id), "blocked_checks": report.get("blocked")},
    )


def _scan_stage(session: Session, tool: RegistryTool) -> StageResult:
    artifact = _artifact_for(session, tool)
    if artifact is None:
        return StageResult(
            stage=STAGE_SCAN,
            status=BLOCKED,
            detail="No artifact, so no scan results.",
            source="runtime_artifact",
        )
    report = json.loads(artifact.validation_json or "{}")
    scans = {
        check["name"]: check["status"]
        for check in report.get("checks", [])
        if check.get("name")
        in ("security_scan", "vulnerability_scan", "secrets", "static_analysis")
    }
    failed = [name for name, status in scans.items() if status == "failed"]
    if failed:
        return StageResult(
            stage=STAGE_SCAN,
            status=BLOCKED,
            detail=f"Scan findings in: {', '.join(failed)}.",
            source="runtime_artifact",
            evidence={"scans": scans},
        )
    blocked = [name for name, status in scans.items() if status == "blocked"]
    return StageResult(
        stage=STAGE_SCAN,
        status=PASSED,
        detail=(
            f"No scan findings. Not run on this install: {', '.join(blocked)}."
            if blocked
            else "No scan findings."
        ),
        source="runtime_artifact",
        evidence={"scans": scans},
    )


def _compliance_stage(session: Session, tool: RegistryTool) -> StageResult:
    """Reads a **real** run. Never runs one implicitly.

    §5.2.11: a compliance scan timeout must *"mark pending; block
    publication"*. That is only expressible if the stage reads a run that
    exists rather than performing one — a stage that ran its own scan would
    have nowhere to put a timeout.
    """
    from sutr.models.governance_run import STATE_COMPLETE, STATE_TIMED_OUT

    run = compliance.latest(session, org_id=tool.org_id, framework=REVIEW_FRAMEWORK)
    if run is None:
        return StageResult(
            stage=STAGE_COMPLIANCE,
            status=PENDING,
            detail=(f"No {REVIEW_FRAMEWORK} compliance run exists. Run one before publication."),
            source="compliance_run",
        )
    if run.state == STATE_TIMED_OUT:
        return StageResult(
            stage=STAGE_COMPLIANCE,
            status=BLOCKED,
            detail=(
                "The compliance scan timed out, so compliance is unknown and publication is "
                f"blocked: {run.failure_reason}"
            ),
            source="compliance_run",
            evidence={"run_id": str(run.id), "state": run.state},
        )
    if run.state != STATE_COMPLETE:
        return StageResult(
            stage=STAGE_COMPLIANCE,
            status=PENDING,
            detail=f"The compliance run is {run.state}.",
            source="compliance_run",
            evidence={"run_id": str(run.id)},
        )
    if run.failed:
        return StageResult(
            stage=STAGE_COMPLIANCE,
            status=BLOCKED,
            detail=f"{run.failed} compliance control(s) failed.",
            source="compliance_run",
            evidence={"run_id": str(run.id), "failed": run.failed, "manual": run.manual},
        )
    return StageResult(
        stage=STAGE_COMPLIANCE,
        status=PASSED,
        detail=(
            f"{run.passed} control(s) passed; {run.manual} are not assessable here and were "
            "not counted as passes."
        ),
        source="compliance_run",
        evidence={"run_id": str(run.id), "passed": run.passed, "manual": run.manual},
    )


def evaluate_stages(session: Session, tool: RegistryTool) -> list[StageResult]:
    """The four evidenced stages, in order. Stops at the first blocker."""
    results: list[StageResult] = []
    for evaluate in (_upload_stage, _validation_stage, _scan_stage, _compliance_stage):
        result = evaluate(session, tool)
        results.append(result)
        if result.status != PASSED:
            break
    return results


# ── The workflow ─────────────────────────────────────────────────────────────


def open_review(
    session: Session,
    *,
    tool: RegistryTool,
    requested_by_user_id: uuid.UUID | None = None,
) -> GovernanceReview:
    """Open a review and run every stage that can be evidenced now."""
    existing = open_for_tool(session, tool.id)
    if existing is not None:
        raise ConflictError(
            f"A review of '{tool.tool_key}' is already open. Decide or withdraw it first."
        )
    review = GovernanceReview(
        org_id=tool.org_id,
        tool_id=tool.id,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(review)
    session.flush()
    refresh(session, review, tool)
    return review


def refresh(session: Session, review: GovernanceReview, tool: RegistryTool) -> GovernanceReview:
    """Re-run the evidenced stages and update where the workflow stands."""
    results = evaluate_stages(session, tool)
    review.stages_json = json.dumps([result.as_dict() for result in results])
    review.updated_at = _utcnow()

    blocker = next((r for r in results if r.status == BLOCKED), None)
    pending = next((r for r in results if r.status == PENDING), None)
    if blocker is not None:
        review.state = STATE_BLOCKED
        review.current_stage = blocker.stage
        review.blocked_reason = blocker.detail
        session.add(review)
        return review

    review.blocked_reason = ""
    if pending is not None:
        review.state = STATE_OPEN
        review.current_stage = pending.stage
        session.add(review)
        return review

    # Every evidenced stage passed. The workflow now waits on manual approval,
    # unless the risk score says it need not.
    assessment = risk.record(session, org_id=tool.org_id, tool=tool)
    review.risk_score = assessment.score
    review.state = STATE_OPEN
    review.current_stage = STAGE_APPROVAL
    session.add(review)
    return review


def stage_results(review: GovernanceReview) -> list[dict[str, Any]]:
    return json.loads(review.stages_json or "[]")


def can_auto_approve(review: GovernanceReview) -> tuple[bool, str]:
    """The LLD's *"low-risk tools can auto-approve"*, made a rule.

    A null score does **not** auto-approve. "Nothing could be measured" is not
    "low risk", and treating it as such would auto-approve exactly the tools
    nobody has looked at.
    """
    if review.state != STATE_OPEN or review.current_stage != STAGE_APPROVAL:
        return False, "The workflow is not waiting on approval."
    if review.risk_score is None:
        return False, (
            "No risk score could be computed, and unknown risk is not low risk. This tool "
            "needs a human decision."
        )
    if review.risk_score >= risk.AUTO_APPROVE_BELOW:
        return False, (
            f"Risk is {review.risk_score}/100, at or above the auto-approval threshold of "
            f"{risk.AUTO_APPROVE_BELOW}."
        )
    return True, (
        f"Risk is {review.risk_score}/100, below the auto-approval threshold of "
        f"{risk.AUTO_APPROVE_BELOW}."
    )


def decide(
    session: Session,
    review: GovernanceReview,
    *,
    approve: bool,
    decided_by_user_id: uuid.UUID | None,
    note: str = "",
) -> GovernanceReview:
    """Record the manual approval, or the refusal."""
    if review.state == STATE_BLOCKED:
        raise ConflictError(
            f"This review is blocked at {review.current_stage}: {review.blocked_reason} "
            "Fix that before deciding."
        )
    if review.state != STATE_OPEN:
        raise ConflictError(f"This review is already {review.state}.")
    if review.current_stage != STAGE_APPROVAL:
        raise ConflictError(f"The workflow is at {review.current_stage}, not manual approval.")
    if decided_by_user_id is not None and review.requested_by_user_id == decided_by_user_id:
        raise ForbiddenError("You opened this review, so somebody else has to decide it.")

    review.state = STATE_APPROVED if approve else STATE_REJECTED
    review.current_stage = STAGE_PUBLICATION if approve else STAGE_APPROVAL
    review.decided_by_user_id = decided_by_user_id
    review.decided_at = _utcnow()
    review.decision_note = note
    review.updated_at = review.decided_at
    session.add(review)
    return review


def auto_approve(session: Session, review: GovernanceReview) -> GovernanceReview:
    allowed, reason = can_auto_approve(review)
    if not allowed:
        raise ConflictError(reason)
    review.state = STATE_APPROVED
    review.current_stage = STAGE_PUBLICATION
    review.auto_approved = True
    review.decision_note = reason
    review.decided_at = _utcnow()
    review.updated_at = review.decided_at
    session.add(review)
    return review


def withdraw(session: Session, review: GovernanceReview) -> GovernanceReview:
    if review.state in (STATE_APPROVED, STATE_REJECTED):
        raise ConflictError(f"This review is already {review.state}.")
    review.state = STATE_WITHDRAWN
    review.updated_at = _utcnow()
    session.add(review)
    return review


def open_for_tool(session: Session, tool_id: uuid.UUID) -> GovernanceReview | None:
    return session.exec(
        select(GovernanceReview)
        .where(GovernanceReview.tool_id == tool_id)
        .where(col(GovernanceReview.state).in_((STATE_OPEN, STATE_BLOCKED)))
    ).first()


def get(session: Session, review_id: uuid.UUID, org_id: uuid.UUID) -> GovernanceReview | None:
    review = session.get(GovernanceReview, review_id)
    if review is None or review.org_id != org_id:
        return None
    return review


def list_reviews(session: Session, *, org_id: uuid.UUID) -> list[GovernanceReview]:
    return list(
        session.exec(
            select(GovernanceReview)
            .where(GovernanceReview.org_id == org_id)
            .order_by(desc(col(GovernanceReview.opened_at)))
        ).all()
    )


def load_tool(session: Session, review: GovernanceReview) -> RegistryTool:
    tool = session.get(RegistryTool, review.tool_id)
    if tool is None:
        raise NotFoundError("The tool this review refers to no longer exists.")
    return tool


def serialize(review: GovernanceReview) -> dict[str, Any]:
    return {
        "id": str(review.id),
        "tool_id": str(review.tool_id),
        "state": review.state,
        "current_stage": review.current_stage,
        "stage_order": list(STAGES),
        "stages": stage_results(review),
        "blocked_reason": review.blocked_reason or None,
        "risk_score": review.risk_score,
        "risk_lower_is_better": True,
        "auto_approved": review.auto_approved,
        "auto_approval": dict(zip(("eligible", "reason"), can_auto_approve(review), strict=True)),
        "decision_note": review.decision_note or None,
        "requested_by_user_id": (
            str(review.requested_by_user_id) if review.requested_by_user_id else None
        ),
        "decided_by_user_id": (
            str(review.decided_by_user_id) if review.decided_by_user_id else None
        ),
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "opened_at": review.opened_at.isoformat(),
    }
