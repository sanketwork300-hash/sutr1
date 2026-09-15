"""Deployment drift: what the platform intends versus what the provider has.

LLD §5.7 asks for *"declarative + continuous reconciliation: drift detection,
version history, one-command rollback, full audit trail"*. Three of those four
already exist here — revisions are the version history, `POST /rollback` is the
one command, and every transition is audited. This module is the fourth.

What it does **not** claim to be is GitOps. There is no Git repository as the
source of truth and no controller reconciling towards it; the desired state is
the deployment row, and this is a comparison run when someone asks, not a loop.
That distinction is recorded in the traceability matrix rather than blurred:
detecting drift and continuously correcting it are different promises.

The comparison is deliberately narrow. Only facts the platform actually
*declared* are checked — which revision should be live, which bytes it should
be running, whether it should be up at all. A provider will always report
fields Sutr never asked for, and reporting those as drift would produce a
detector that is never clean and therefore never read.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from sutr.deploy.base import ProviderError
from sutr.deploy.credentials import resolve_target
from sutr.deploy.registry import get_provider
from sutr.models.deployment import Deployment
from sutr.services.deployments import get_revision, package_digest

# What a deployment's status is supposed to mean about the provider's state.
_EXPECTED_PROVIDER_STATE = {
    "running": "running",
    "stopped": "stopped",
}


@dataclass
class Finding:
    code: str
    detail: str
    desired: Any = None
    observed: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "desired": self.desired,
            "observed": self.observed,
        }


@dataclass
class DriftReport:
    checked: bool = True
    findings: list[Finding] = field(default_factory=list)
    unavailable_reason: str | None = None

    @property
    def drifted(self) -> bool:
        return bool(self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "drifted": self.drifted,
            "findings": [finding.as_dict() for finding in self.findings],
            "unavailable_reason": self.unavailable_reason,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if not self.checked:
            return self.unavailable_reason or "The deployment could not be checked."
        if not self.findings:
            return "The provider matches what the platform declared."
        return f"{len(self.findings)} difference(s) between the declared state and the provider."


async def detect(session: Session, deployment: Deployment) -> DriftReport:
    """Compare one deployment's declared state against its provider."""
    report = DriftReport()
    provider = get_provider(deployment.provider)
    if provider is None:
        return DriftReport(
            checked=False,
            unavailable_reason=(
                f"The '{deployment.provider}' provider is not available on this instance, so "
                "its deployments cannot be checked."
            ),
        )

    state = json.loads(deployment.provider_state_json or "{}")
    if not state:
        return DriftReport(
            checked=False,
            unavailable_reason="This deployment has never reached a provider.",
        )

    # ── The revision the provider is running ────────────────────────────────
    recorded = state.get("revision")
    if recorded is not None and int(recorded) != deployment.current_revision:
        report.findings.append(
            Finding(
                code="revision_mismatch",
                detail=(
                    "The provider is running a different revision than the one the platform "
                    "believes is live."
                ),
                desired=deployment.current_revision,
                observed=int(recorded),
            )
        )

    # ── The bytes that revision should be running ───────────────────────────
    active = get_revision(session, deployment.id, deployment.current_revision)
    if active is not None and active.package_sha256:
        current = package_digest(deployment.package_zip)
        if current != active.package_sha256:
            report.findings.append(
                Finding(
                    code="package_mismatch",
                    detail=(
                        "The deployment's stored package is not the package recorded for its "
                        "live revision."
                    ),
                    desired=active.package_sha256,
                    observed=current,
                )
            )

    # ── Whether it is up ────────────────────────────────────────────────────
    try:
        target = await resolve_target(session, deployment)
        observed = await provider.status(state, target)
    except ProviderError as exc:
        return DriftReport(
            checked=False,
            findings=report.findings,
            unavailable_reason=f"The provider could not be reached: {exc}",
        )

    expected = _EXPECTED_PROVIDER_STATE.get(deployment.status)
    if expected and observed.state != expected:
        report.findings.append(
            Finding(
                code="state_mismatch",
                detail=observed.detail
                or f"The platform records '{deployment.status}'; the provider reports "
                f"'{observed.state}'.",
                desired=expected,
                observed=observed.state,
            )
        )
    return report
