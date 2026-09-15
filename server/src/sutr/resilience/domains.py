"""Failure domains and severities (LLD §5.8).

§5.8 asks for eight failure domains "each with independent recovery" and four
severities. Both are classification schemes, and a classification scheme that
lives in a wiki is one nobody applies at 3am.

So they live here, next to the code that fails, and two things use them: the
capability report, so an operator can read the taxonomy this build was written
against, and the resilience tests, which assert that each domain names the
degradation this platform actually performs when that domain fails.

The value is not the enum. It is the third column: for each domain, *what keeps
working*. That is the sentence that turns "the database is down" into a
decision about whether to page someone.
"""

from dataclasses import dataclass

# ── Severity (LLD §5.8) ──────────────────────────────────────────────────────

P0 = "P0"  # platform down
P1 = "P1"  # critical service degraded
P2 = "P2"  # functional degradation
P3 = "P3"  # minor

SEVERITIES = {
    P0: "The platform is not serving agent traffic.",
    P1: "A critical service is degraded; agent traffic is affected but served.",
    P2: "A function is degraded; other functions are unaffected.",
    P3: "Minor: noticed, not urgent.",
}


@dataclass(frozen=True)
class Domain:
    """One failure domain, and what this build does when it fails."""

    name: str
    severity: str
    # What breaks.
    effect: str
    # What keeps working, which is the half worth writing down.
    survives: str
    # Where the handling lives, so the claim can be checked.
    implemented_in: str


DOMAINS = (
    Domain(
        name="application",
        severity=P0,
        effect="A replica stops serving.",
        survives=(
            "Other replicas serve. The load balancer drops it on its readiness probe, and "
            "any singleton job it held moves to another replica once its lease expires."
        ),
        implemented_in="api/health.py, platform/leadership.py",
    ),
    Domain(
        name="database",
        severity=P0,
        effect="Nothing can be read or written.",
        survives=(
            "Nothing meaningful — this is the one dependency with no fallback. Replicas "
            "report not-ready rather than serving errors, and background jobs stop rather "
            "than half-running."
        ),
        implemented_in="api/health.py, platform/leadership.py",
    ),
    Domain(
        name="messaging",
        severity=P2,
        effect="Events are not delivered to consumers.",
        survives=(
            "Everything. Events are written to the transactional outbox in the same "
            "transaction as the change they describe, so a bus that is down delays "
            "delivery rather than losing facts."
        ),
        implemented_in="events/outbox.py, events/relay.py",
    ),
    Domain(
        name="external_provider",
        severity=P2,
        effect="One provider's tools fail.",
        survives=(
            "Every other provider. After repeated failures the circuit opens and calls "
            "fail immediately with a named error instead of occupying a worker for the "
            "full timeout."
        ),
        implemented_in="resilience/breaker.py",
    ),
    Domain(
        name="runtime",
        severity=P2,
        effect="A generated MCP server is down.",
        survives=(
            "Every other tool. The deployment sweep observes the status and reports it; "
            "nothing else is affected."
        ),
        implemented_in="maintenance.py, deploy/",
    ),
    Domain(
        name="control_plane",
        severity=P1,
        effect="Onboarding, translation, generation and publication stop.",
        survives=(
            "Live agent traffic. The data plane does not import control-plane modules, "
            "which is enforced by a test rather than by intention (ADR-002)."
        ),
        implemented_in="platform/boundaries.py",
    ),
    Domain(
        name="discovery",
        severity=P2,
        effect="Intent-to-tool search degrades.",
        survives=(
            "Provisioned tools keep executing, and search itself degrades in named steps "
            "— graph skipped, lexical only, retrieval order — each of which is reported "
            "in the response rather than silently applied."
        ),
        implemented_in="discovery/service.py",
    ),
    Domain(
        name="region",
        severity=P1,
        effect="A whole region is unreachable.",
        survives=(
            "Other regions, to the extent they are deployed. A standby serves reads and "
            "refuses writes with a named 503; nothing here promotes it — that is a "
            "database operation (ADR-075)."
        ),
        implemented_in="platform/mode.py",
    ),
)


def describe() -> dict:
    return {
        "severities": SEVERITIES,
        "domains": [
            {
                "name": domain.name,
                "severity": domain.severity,
                "effect": domain.effect,
                "survives": domain.survives,
                "implemented_in": domain.implemented_in,
            }
            for domain in DOMAINS
        ],
    }
