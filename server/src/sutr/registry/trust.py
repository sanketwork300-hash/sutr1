"""The trust score: 0–100, over seven named inputs, and explainable.

LLD §3.7 specifies both the inputs and the property that matters:

    Trust score (0–100, e.g. 94). Inputs: security scans · validation success ·
    runtime availability · error rate · governance status · doc quality · user
    ratings. Influences search ranking.

A single number that ranks tools is exactly the kind of number that gets
believed without being understood, so the whole design here is about making it
arguable.

**Unmeasurable inputs are excluded, not defaulted.** A tool nobody has reviewed
has no rating; scoring that as zero would punish a new tool for being new, and
scoring it as one would flatter it. So an unavailable component drops out of
both the numerator and the denominator, its absence is named, and the result
carries a `coverage` figure saying what fraction of the possible weight was
actually measured.

**No measurable input at all means no score.** `None` with a reason, never 0.
A tool nothing is known about is not a tool known to be bad — that is the same
rule the marketplace already applies to its null fields, applied to the number
that would be hardest to argue with.

**Every component reports its own evidence.** Not just a value, but the counts
it was computed from, so a provider disputing their score can see whether the
platform is looking at the right deployments.

Reads cross service boundaries (artifacts, deployments, logs, documents,
reviews) and every one of them is a single-table, org-scoped select. No joins
across an owner's tables: the boundary rule is about who *writes*, and reading
one table at a time keeps it obvious that nothing here writes anything.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from sutr.models.document import Document
from sutr.models.marketplace_review import MarketplaceReview
from sutr.models.registry_change_request import STATUS_PENDING, RegistryChangeRequest
from sutr.models.registry_tool import (
    ACTIVE,
    APPROVED,
    ARCHIVED,
    DEPRECATED,
    PUBLISHED,
    REJECTED,
    SUSPENDED,
    UNDER_REVIEW,
    RegistryTool,
)
from sutr.models.runtime_artifact import STATUS_VALIDATED, RuntimeArtifact

# Weights. They sum to 100 so a component's weight reads directly as "up to
# this many points", which is the only property that makes the arithmetic
# checkable by eye.
WEIGHTS: dict[str, int] = {
    "security_scans": 20,
    "validation_success": 20,
    "runtime_availability": 15,
    "error_rate": 15,
    "governance_status": 10,
    "doc_quality": 10,
    "user_ratings": 10,
}

# How far back the operational components look. Long enough to have data,
# short enough that a tool that broke last week does not keep last month's
# score.
WINDOW_DAYS = 30

# Below this many observations the operational components abstain rather than
# scoring: three calls is not an error rate.
MIN_OBSERVATIONS = 5

NO_INPUTS = (
    "No trust input could be measured for this tool yet — no build, no deployment, no calls, "
    "no documentation and no ratings. The score is unknown rather than zero."
)


@dataclass
class Component:
    """One input to the score, with its evidence and its abstentions."""

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
class TrustScore:
    score: int | None
    components: list[Component]
    coverage: float
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "max": 100,
            # What fraction of the total weight was actually measured. A score
            # of 90 at 30% coverage and one at 100% coverage are different
            # claims, and a reader should be able to tell them apart.
            "coverage": round(self.coverage, 3),
            "unavailable_reason": self.unavailable_reason,
            "components": [component.as_dict() for component in self.components],
            "explanation": self.explain(),
        }

    def explain(self) -> str:
        if self.score is None:
            return self.unavailable_reason or NO_INPUTS
        measured = [c for c in self.components if c.available]
        missing = [c.name for c in self.components if not c.available]
        parts = ", ".join(f"{c.name} {c.points:.0f}/{c.weight}" for c in measured)
        sentence = f"{self.score}/100 from {parts}."
        if missing:
            sentence += f" Not measured: {', '.join(missing)}."
        return sentence


def _window_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)


def _artifacts(session: Session, tool: RegistryTool) -> list[RuntimeArtifact]:
    if tool.project_id is None:
        return []
    return list(
        session.exec(
            select(RuntimeArtifact)
            .where(RuntimeArtifact.org_id == tool.org_id)
            .where(RuntimeArtifact.project_id == tool.project_id)
        ).all()
    )


def _security_scans(artifacts: list[RuntimeArtifact]) -> Component:
    """Did the scans pass — and were they even run?

    A `blocked` scan is the whole reason this component can abstain: an install
    with no scanner has not established that a tool is clean, and scoring it as
    if it had would launder a gap into a number.
    """
    component = Component("security_scans", WEIGHTS["security_scans"])
    if not artifacts:
        component.unavailable_reason = "No runtime artifact has been built for this tool."
        return component
    latest = max(artifacts, key=lambda a: a.created_at)
    report = json.loads(latest.validation_json or "{}")
    scanning_checks = ("security_scan", "vulnerability_scan", "secrets", "static_analysis")
    checks = {
        check["name"]: check["status"]
        for check in report.get("checks", [])
        if check.get("name") in scanning_checks
    }
    ran = {name: status for name, status in checks.items() if status in ("ok", "failed")}
    if not ran:
        component.unavailable_reason = (
            "No security scan has run on this tool's artifact — the scanners are not configured "
            "on this install, so nothing has been established either way."
        )
        component.detail = {"checks": checks}
        return component
    passed = sum(1 for status in ran.values() if status == "ok")
    component.available = True
    component.value = passed / len(ran)
    component.detail = {"checks": checks, "ran": len(ran), "passed": passed}
    return component


def _validation_success(artifacts: list[RuntimeArtifact]) -> Component:
    component = Component("validation_success", WEIGHTS["validation_success"])
    if not artifacts:
        component.unavailable_reason = "No runtime artifact has been built for this tool."
        return component
    validated = sum(1 for a in artifacts if a.status == STATUS_VALIDATED)
    component.available = True
    component.value = validated / len(artifacts)
    component.detail = {"artifacts": len(artifacts), "validated": validated}
    return component


def _runtime_availability(session: Session, tool: RegistryTool) -> Component:
    from sutr.models.deployment import Deployment

    component = Component("runtime_availability", WEIGHTS["runtime_availability"])
    if tool.project_id is None:
        component.unavailable_reason = "This tool has no source project, so it has no deployments."
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
    healthy = sum(1 for d in deployments if d.status == "running")
    failed = sum(1 for d in deployments if d.status == "failed")
    component.available = True
    component.value = healthy / len(deployments)
    component.detail = {"deployments": len(deployments), "running": healthy, "failed": failed}
    return component


def _error_rate(session: Session, tool: RegistryTool) -> Component:
    from sutr.models.log import LogEntry

    component = Component("error_rate", WEIGHTS["error_rate"])
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
            f"Only {total} call(s) in the last {WINDOW_DAYS} days — too few to be an error rate."
        )
        component.detail = {"calls": total, "minimum": MIN_OBSERVATIONS}
        return component
    errors = counts.get("error", 0)
    component.available = True
    # Inverted: the score rewards a low error rate.
    component.value = 1.0 - (errors / total)
    component.detail = {"calls": total, "errors": errors, "window_days": WINDOW_DAYS}
    return component


def _governance_status(session: Session, tool: RegistryTool) -> Component:
    """Where the tool stands with governance. Always measurable — it is state.

    A tool that governance rejected or suspended scores zero here and that is
    the point: it is the one component that can move on a decision rather than
    on a measurement.
    """
    component = Component("governance_status", WEIGHTS["governance_status"])
    pending = session.exec(
        select(func.count())
        .select_from(RegistryChangeRequest)
        .where(RegistryChangeRequest.tool_id == tool.id)
        .where(RegistryChangeRequest.status == STATUS_PENDING)
    ).one()
    standing = {
        ACTIVE: 1.0,
        PUBLISHED: 1.0,
        APPROVED: 0.9,
        UNDER_REVIEW: 0.6,
        DEPRECATED: 0.3,
        SUSPENDED: 0.0,
        REJECTED: 0.0,
        ARCHIVED: 0.0,
    }
    component.available = True
    component.value = standing.get(tool.lifecycle_state, 0.5)
    component.detail = {"lifecycle_state": tool.lifecycle_state, "pending_change_requests": pending}
    return component


def _doc_quality(session: Session, tool: RegistryTool) -> Component:
    """Three things a reader needs, counted rather than judged.

    A summary, a description, and documentation the platform has actually
    processed. This is a completeness measure and says so — nothing here reads
    the prose and forms an opinion of it.
    """
    component = Component("doc_quality", WEIGHTS["doc_quality"])
    documents = 0
    if tool.project_id is not None:
        documents = session.exec(
            select(func.count())
            .select_from(Document)
            .where(Document.org_id == tool.org_id)
            .where(Document.project_id == tool.project_id)
        ).one()
    signals = {
        "has_summary": bool(tool.summary.strip()),
        "has_description": len(tool.description.strip()) >= 80,
        "has_documentation": documents > 0,
    }
    component.available = True
    component.value = sum(1 for present in signals.values() if present) / len(signals)
    component.detail = {**signals, "documents": documents, "measures": "completeness, not prose"}
    return component


def _user_ratings(session: Session, tool: RegistryTool) -> Component:
    component = Component("user_ratings", WEIGHTS["user_ratings"])
    if not tool.integration_id:
        component.unavailable_reason = "This tool has no marketplace identity yet, so no ratings."
        return component
    rows = session.exec(
        select(func.avg(MarketplaceReview.rating), func.count()).where(
            MarketplaceReview.integration_id == tool.integration_id
        )
    ).one()
    average, count = rows
    if not count:
        component.unavailable_reason = "Nobody has rated this tool."
        return component
    # 1–5 stars onto 0–1, so one star is zero rather than a fifth of the marks.
    component.available = True
    component.value = (float(average) - 1.0) / 4.0
    component.detail = {"average": round(float(average), 2), "reviews": int(count)}
    return component


def compute(session: Session, tool: RegistryTool) -> TrustScore:
    """The score, its components, and how much of it was actually measured."""
    artifacts = _artifacts(session, tool)
    components = [
        _security_scans(artifacts),
        _validation_success(artifacts),
        _runtime_availability(session, tool),
        _error_rate(session, tool),
        _governance_status(session, tool),
        _doc_quality(session, tool),
        _user_ratings(session, tool),
    ]
    measured = [component for component in components if component.available]
    measurable_weight = sum(component.weight for component in measured)
    total_weight = sum(WEIGHTS.values())
    if not measurable_weight:
        return TrustScore(
            score=None, components=components, coverage=0.0, unavailable_reason=NO_INPUTS
        )
    earned = sum(component.points for component in measured)
    return TrustScore(
        score=round(100 * earned / measurable_weight),
        components=components,
        coverage=measurable_weight / total_weight,
    )


def compute_for_id(session: Session, tool_id: uuid.UUID) -> TrustScore | None:
    tool = session.get(RegistryTool, tool_id)
    return compute(session, tool) if tool is not None else None
