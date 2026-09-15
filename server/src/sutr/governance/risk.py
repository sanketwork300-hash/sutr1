"""The risk engine: a score per tool, where **lower is better**.

LLD §5.2.7: *"dynamic score per provider/tool (lower = better, e.g. 18/100) from
scan findings, CVEs, verification status, stability, incidents, violations, data
sensitivity."*

The direction is the first thing to get right and the easiest to get wrong.
Phase 6's trust score runs the other way — higher is better — and the two are
about the same tools. So every response here says `"lower_is_better": true`, and
nothing in the codebase converts between them: a number that means the opposite
of what a reader assumes is worse than no number.

The rest of the design is Phase 6's, because the problem is the same one.
Weights sum to 100 so a weight reads as "up to this many risk points". A
component that cannot be measured **abstains** rather than defaulting: scoring
an unmeasured input as zero risk flatters a tool nobody has looked at, and
scoring it as maximum risk condemns one for being new. `coverage` reports what
fraction of the total weight was actually measured, and with nothing measurable
the score is `None` with a reason — never 0, which here would mean "no risk at
all".

One consequence of that normalisation is worth stating because it surprises
people: **two scores are only comparable at similar coverage.** A score is risk
*out of what was measured*, so a component becoming measurable changes the
denominator — and a newly-measured low-risk component can lower the score even
though the underlying facts got worse. Read `coverage` alongside the number, the
way `capabilities` endpoints elsewhere are read alongside a status.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, desc, select

from sutr.models.governance_run import RiskAssessment

# The seven inputs the LLD names. Weights sum to 100.
WEIGHTS: dict[str, int] = {
    "scan_findings": 20,
    "cves": 15,
    "verification_status": 15,
    "stability": 15,
    "incidents": 10,
    "violations": 15,
    "data_sensitivity": 10,
}

WINDOW_DAYS = 30
MIN_OBSERVATIONS = 5

# Below this, the approval workflow may auto-approve (LLD §5.2.8: *"low-risk
# tools can auto-approve"*). Deliberately strict: an auto-approval is a human
# review that did not happen.
AUTO_APPROVE_BELOW = 25

NO_INPUTS = (
    "No risk input could be measured for this tool yet — no build, no calls, no incidents and "
    "no violations. The score is unknown rather than zero, because zero here would mean no "
    "risk at all."
)

# Categories a provider may declare for the data a tool handles, and how much
# risk each contributes. A declaration, not a finding: nothing inspects payloads.
SENSITIVITY = {
    "none": 0.0,
    "internal": 0.25,
    "personal": 0.6,
    "financial": 0.8,
    "health": 1.0,
}


@dataclass
class Component:
    name: str
    weight: int
    value: float | None = None
    available: bool = False
    unavailable_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def points(self) -> float:
        return (self.value or 0.0) * self.weight if self.available else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": self.weight,
            "value": round(self.value, 4) if self.value is not None else None,
            "points": round(self.points, 2) if self.available else None,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "detail": self.detail,
        }


@dataclass
class Risk:
    score: int | None
    components: list[Component]
    coverage: float
    unavailable_reason: str | None = None

    @property
    def low(self) -> bool:
        return self.score is not None and self.score < AUTO_APPROVE_BELOW

    def explain(self) -> str:
        if self.score is None:
            return self.unavailable_reason or NO_INPUTS
        measured = [c for c in self.components if c.available]
        missing = [c.name for c in self.components if not c.available]
        parts = ", ".join(f"{c.name} {c.points:.0f}/{c.weight}" for c in measured)
        sentence = f"{self.score}/100 risk from {parts}."
        if missing:
            sentence += f" Not measured: {', '.join(missing)}."
        return sentence

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "max": 100,
            "lower_is_better": True,
            "coverage": round(self.coverage, 3),
            "auto_approve_threshold": AUTO_APPROVE_BELOW,
            "low_risk": self.low,
            "unavailable_reason": self.unavailable_reason,
            "components": [component.as_dict() for component in self.components],
            "explanation": self.explain(),
        }


def _window_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)


def _artifacts(session: Session, tool) -> list:
    from sutr.models.runtime_artifact import RuntimeArtifact

    if tool.project_id is None:
        return []
    return list(
        session.exec(
            select(RuntimeArtifact)
            .where(RuntimeArtifact.org_id == tool.org_id)
            .where(RuntimeArtifact.project_id == tool.project_id)
        ).all()
    )


def _scan_findings(artifacts: list) -> Component:
    """Findings from the checks that actually ran on the latest artifact."""
    component = Component("scan_findings", WEIGHTS["scan_findings"])
    if not artifacts:
        component.unavailable_reason = "No runtime artifact has been built for this tool."
        return component
    latest_artifact = max(artifacts, key=lambda a: a.created_at)
    report = json.loads(latest_artifact.validation_json or "{}")
    findings = 0
    ran = 0
    for check in report.get("checks", []):
        if check.get("status") in ("ok", "failed"):
            ran += 1
            findings += len(check.get("findings") or [])
    if not ran:
        component.unavailable_reason = "No validation check ran on this tool's artifact."
        return component
    component.available = True
    # Saturating: five findings and fifty are both "a lot", and a linear scale
    # would let one noisy check dominate the whole score.
    component.value = min(findings, 5) / 5
    component.detail = {"findings": findings, "checks_run": ran}
    return component


def _cves(artifacts: list) -> Component:
    """Vulnerabilities from the dependency scan — when one has run."""
    component = Component("cves", WEIGHTS["cves"])
    if not artifacts:
        component.unavailable_reason = "No runtime artifact has been built for this tool."
        return component
    latest_artifact = max(artifacts, key=lambda a: a.created_at)
    report = json.loads(latest_artifact.validation_json or "{}")
    scan = next(
        (c for c in report.get("checks", []) if c.get("name") == "vulnerability_scan"), None
    )
    if scan is None or scan.get("status") == "blocked":
        component.unavailable_reason = (
            "No vulnerability scan has run on this tool — no scanner is configured on this "
            "install, so nothing is established either way."
        )
        component.detail = {"status": (scan or {}).get("status")}
        return component
    component.available = True
    component.value = 1.0 if scan.get("status") == "failed" else 0.0
    component.detail = {"status": scan.get("status")}
    return component


def _verification(session: Session, tool) -> Component:
    """Is the provider verified, and did the build pass validation?"""
    from sutr.marketplace import profiles
    from sutr.models.runtime_artifact import STATUS_VALIDATED

    component = Component("verification_status", WEIGHTS["verification_status"])
    profile = profiles.get(session, tool.org_id)
    artifacts = _artifacts(session, tool)
    validated = [a for a in artifacts if a.status == STATUS_VALIDATED]
    signals = {
        "provider_verified": bool(profile and profile.verified),
        "has_validated_artifact": bool(validated),
    }
    component.available = True
    # Risk falls as verification rises.
    component.value = 1.0 - (sum(1 for present in signals.values() if present) / len(signals))
    component.detail = signals
    return component


def _stability(session: Session, tool) -> Component:
    """How often calls to this tool fail."""
    from sutr.models.log import LogEntry

    component = Component("stability", WEIGHTS["stability"])
    if not tool.integration_id:
        component.unavailable_reason = (
            "This tool is not served under an integration id yet, so no calls are attributed to it."
        )
        return component
    rows = session.exec(
        select(LogEntry.outcome, func.count())
        .where(LogEntry.org_id == tool.org_id)
        .where(LogEntry.integration_id == tool.integration_id)
        .where(col(LogEntry.timestamp) >= _window_start())
        .group_by(col(LogEntry.outcome))
    ).all()
    counts = {outcome or "unknown": int(count) for outcome, count in rows}
    total = sum(counts.values())
    if total < MIN_OBSERVATIONS:
        component.unavailable_reason = (
            f"Only {total} call(s) in the last {WINDOW_DAYS} days — too few to say anything "
            "about stability."
        )
        component.detail = {"calls": total, "minimum": MIN_OBSERVATIONS}
        return component
    errors = counts.get("error", 0)
    component.available = True
    component.value = errors / total
    component.detail = {"calls": total, "errors": errors, "window_days": WINDOW_DAYS}
    return component


def _incidents(session: Session, tool) -> Component:
    """Deployments that failed. The closest thing to an incident record here."""
    from sutr.models.deployment import Deployment

    component = Component("incidents", WEIGHTS["incidents"])
    if tool.project_id is None:
        component.unavailable_reason = "This tool has no source project, so it has no runtimes."
        return component
    deployments = list(
        session.exec(
            select(Deployment)
            .where(Deployment.org_id == tool.org_id)
            .where(Deployment.project_id == tool.project_id)
        ).all()
    )
    if not deployments:
        component.unavailable_reason = "This tool has never been deployed."
        return component
    failed = sum(1 for d in deployments if d.status == "failed")
    component.available = True
    component.value = failed / len(deployments)
    component.detail = {"deployments": len(deployments), "failed": failed}
    return component


def _violations(session: Session, tool) -> Component:
    """Calls refused by policy, and exceptions raised against this tool."""
    from sutr.models.governance_exception import STATE_APPROVED, GovernanceException
    from sutr.models.log import LogEntry

    component = Component("violations", WEIGHTS["violations"])
    # Counted in the database rather than by loading every row and taking its
    # length: a tenant with a thousand exceptions should cost one integer, not
    # a thousand objects.
    exceptions = session.exec(
        select(func.count())
        .select_from(GovernanceException)
        .where(GovernanceException.org_id == tool.org_id)
        .where(GovernanceException.scope_id == str(tool.id))
        .where(GovernanceException.state == STATE_APPROVED)
    ).one()
    denied = 0
    total = 0
    if tool.integration_id:
        rows = session.exec(
            select(LogEntry.outcome, func.count())
            .where(LogEntry.org_id == tool.org_id)
            .where(LogEntry.integration_id == tool.integration_id)
            .where(col(LogEntry.timestamp) >= _window_start())
            .group_by(col(LogEntry.outcome))
        ).all()
        counts = {outcome or "unknown": int(count) for outcome, count in rows}
        total = sum(counts.values())
        denied = counts.get("denied", 0)

    if not total and not exceptions:
        component.unavailable_reason = "No policy refusals and no exceptions recorded."
        return component
    component.available = True
    # An approved exception is a documented deviation, so it carries risk — but
    # less than a refusal nobody looked at.
    ratio = (denied / total) if total else 0.0
    component.value = min(1.0, ratio + 0.25 * exceptions)
    component.detail = {"denied": denied, "calls": total, "open_exceptions": exceptions}
    return component


def _sensitivity(tool) -> Component:
    """What the provider says this tool handles. A declaration, not a finding."""
    component = Component("data_sensitivity", WEIGHTS["data_sensitivity"])
    declared = [claim.lower() for claim in json.loads(tool.compliance_json or "[]")]
    # A provider claiming PCI DSS or HIPAA is telling you the data is
    # sensitive, whatever else the claim means.
    level = "none"
    if any("hipaa" in claim or "health" in claim for claim in declared):
        level = "health"
    elif any("pci" in claim or "financial" in claim for claim in declared):
        level = "financial"
    elif any("gdpr" in claim or "dpdp" in claim or "personal" in claim for claim in declared):
        level = "personal"
    elif declared:
        level = "internal"
    component.available = True
    component.value = SENSITIVITY[level]
    component.detail = {
        "level": level,
        "declared": declared,
        "source": "the provider's own compliance declarations; nothing inspects payloads",
    }
    return component


def compute(session: Session, tool) -> Risk:
    """The risk score for one registry tool, with every component's evidence."""
    artifacts = _artifacts(session, tool)
    components = [
        _scan_findings(artifacts),
        _cves(artifacts),
        _verification(session, tool),
        _stability(session, tool),
        _incidents(session, tool),
        _violations(session, tool),
        _sensitivity(tool),
    ]
    measured = [component for component in components if component.available]
    measurable_weight = sum(component.weight for component in measured)
    total_weight = sum(WEIGHTS.values())
    if not measurable_weight:
        return Risk(score=None, components=components, coverage=0.0, unavailable_reason=NO_INPUTS)
    earned = sum(component.points for component in measured)
    return Risk(
        score=round(100 * earned / measurable_weight),
        components=components,
        coverage=measurable_weight / total_weight,
    )


def record(session: Session, *, org_id: uuid.UUID, tool, failure: str = "") -> RiskAssessment:
    """Compute and store an assessment. The caller commits.

    `failure` triggers §5.2.11's *"Risk calc failure — keep previous score, flag
    recalc"*: the existing row is marked stale with a reason rather than
    overwritten with nothing. A score that vanishes on a failed recalculation
    would make every consumer treat the tool as unknown, which is a different
    claim from "we could not refresh this".
    """
    existing = latest(session, org_id=org_id, target_id=str(tool.id))
    if failure:
        if existing is None:
            assessment = RiskAssessment(
                org_id=org_id,
                target_type="registry_tool",
                target_id=str(tool.id),
                score=None,
                unavailable_reason=failure,
                stale=True,
                stale_reason=failure,
            )
            session.add(assessment)
            session.flush()
            return assessment
        existing.stale = True
        existing.stale_reason = failure
        session.add(existing)
        return existing

    result = compute(session, tool)
    assessment = RiskAssessment(
        org_id=org_id,
        target_type="registry_tool",
        target_id=str(tool.id),
        score=result.score,
        components_json=json.dumps([component.as_dict() for component in result.components]),
        coverage=result.coverage,
        unavailable_reason=result.unavailable_reason or "",
    )
    session.add(assessment)
    session.flush()
    return assessment


def latest(session: Session, *, org_id: uuid.UUID, target_id: str) -> RiskAssessment | None:
    return session.exec(
        select(RiskAssessment)
        .where(RiskAssessment.org_id == org_id)
        .where(RiskAssessment.target_id == target_id)
        .order_by(desc(col(RiskAssessment.computed_at)))
    ).first()


def serialize(assessment: RiskAssessment) -> dict[str, Any]:
    return {
        "id": str(assessment.id),
        "target_type": assessment.target_type,
        "target_id": assessment.target_id,
        "score": assessment.score,
        "max": 100,
        "lower_is_better": True,
        "coverage": round(assessment.coverage, 3),
        "low_risk": assessment.score is not None and assessment.score < AUTO_APPROVE_BELOW,
        "stale": assessment.stale,
        "stale_reason": assessment.stale_reason or None,
        "unavailable_reason": assessment.unavailable_reason or None,
        "components": json.loads(assessment.components_json or "[]"),
        "computed_at": assessment.computed_at.isoformat(),
    }


def describe() -> dict[str, Any]:
    return {
        "lower_is_better": True,
        "max": 100,
        "auto_approve_threshold": AUTO_APPROVE_BELOW,
        "weights": dict(WEIGHTS),
        "note": (
            "Lower is better here, the opposite of the registry's trust score. Nothing converts "
            "between the two. An unmeasurable input abstains rather than defaulting to zero "
            "risk, and with nothing measurable the score is null."
        ),
        "comparability": (
            "A score is risk out of what was measured, so two scores are only comparable at "
            "similar coverage. A component becoming measurable changes the denominator and can "
            "move the number in either direction."
        ),
    }
