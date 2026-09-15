"""The governance points, and what happens when governance itself fails.

LLD §5.2 opens with the coverage claim — *"Continuous governance across both
planes: upload → validation → generation → deployment → publication →
invocation → monitoring"* — and closes with the rule that matters most:
*"Governance failures never silently permit unauthorized actions — fail closed
for high-risk operations."*

This module answers two questions honestly.

**Which of the seven points is governed?** Not by asserting seven, but by
naming, for each, the code that actually gates it and what happens when nothing
does. Five are gated today. `describe()` lists them with the gate, and the two
that are not are said to be not.

**What happens when governance itself fails?** §5.2.11 gives four rows, and each
is implemented where the failure occurs rather than as a generic handler:

| failure                 | behaviour                                          |
|-------------------------|----------------------------------------------------|
| compliance scan timeout | mark pending, block publication                    |
| risk calc failure       | keep the previous score, flag it stale             |
| audit write failure     | the whole transaction fails, so the act does not   |
| policy deploy failure   | roll back to the last active version               |

The audit row is the one worth reading twice. The LLD asks for *"queue durably;
retry before completing sensitive ops"*. Sutr writes the audit row **in the same
transaction as the action it describes**, so a failed audit write rolls the
action back with it. That is stronger than a queue: there is no window in which
the act completed and the record is still in flight (ADR-060).
"""

from typing import Any

# The seven points §5.2 names, and what gates each one here.
POINTS: tuple[dict[str, Any], ...] = (
    {
        "point": "upload",
        "gated": True,
        "gate": "openapi/pipeline.py — parse, validate and lint before an IR exists",
        "fails_closed": True,
    },
    {
        "point": "validation",
        "gated": True,
        "gate": "generation/validation.py — seven checks; a failure rejects the artifact",
        "fails_closed": True,
    },
    {
        "point": "generation",
        "gated": True,
        "gate": "generation/pipeline.py — a rejected artifact is stored but not deployable",
        "fails_closed": True,
    },
    {
        "point": "deployment",
        "gated": True,
        "gate": "api/deployments.py — only a validated artifact may be deployed",
        "fails_closed": True,
    },
    {
        "point": "publication",
        "gated": True,
        "gate": (
            "registry/service.py — publication is a governance-gated transition; "
            "governance/review.py evidences the six-stage workflow"
        ),
        "fails_closed": True,
    },
    {
        "point": "invocation",
        "gated": True,
        "gate": (
            "services/tool_pipeline.py — quota, then the four authorization layers, then the "
            "per-tool policy"
        ),
        "fails_closed": True,
    },
    {
        "point": "monitoring",
        "gated": False,
        "gate": None,
        "fails_closed": False,
        "detail": (
            "NOT IMPLEMENTED: nothing evaluates governance continuously against a running "
            "tool. Drift detection and metrics exist, but no policy is evaluated on a schedule "
            "and no finding is raised from one."
        ),
    },
)

FAIL_SAFE: tuple[dict[str, Any], ...] = (
    {
        "failure": "compliance_scan_timeout",
        "behaviour": "Marked timed_out; the review's compliance stage blocks publication.",
        "implemented_in": "governance/compliance.py, governance/review.py",
    },
    {
        "failure": "risk_calculation_failure",
        "behaviour": (
            "The previous assessment is kept and flagged stale with the reason. A score is "
            "never overwritten with nothing."
        ),
        "implemented_in": "governance/risk.py",
    },
    {
        "failure": "audit_write_failure",
        "behaviour": (
            "The audit row is written in the same transaction as the action, so a failed "
            "write rolls the action back. Stronger than queue-and-retry: there is no window "
            "in which the act completed and the record is in flight."
        ),
        "implemented_in": "services/audit.py",
    },
    {
        "failure": "policy_deploy_failure",
        "behaviour": (
            "The previously active version is restored by moving the pointer back. "
            "`policies.rollback` is the inverse of `policies.activate`."
        ),
        "implemented_in": "governance/policies.py",
    },
)


def describe() -> dict[str, Any]:
    from sutr.governance import compliance, exceptions, policies, risk

    gated = [point for point in POINTS if point["gated"]]
    return {
        "points": list(POINTS),
        "points_gated": len(gated),
        "points_total": len(POINTS),
        "fail_safe": list(FAIL_SAFE),
        "policies": policies.describe(),
        "compliance": compliance.describe(),
        "risk": risk.describe(),
        "exceptions": exceptions.describe(),
        "never_silently_permits": (
            "Every gate above refuses on failure. Where a check cannot run — no scanner, no "
            "compliance run — the result is `blocked` or `pending`, never a pass."
        ),
    }
