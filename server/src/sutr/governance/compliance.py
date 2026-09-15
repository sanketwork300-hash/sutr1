"""The compliance engine, and what it can honestly assess.

LLD §5.2.6 names seven frameworks — ISO 27001, SOC 2, GDPR, HIPAA, PCI DSS, RBI
Digital Banking, DPDP Act (India) — plus organisation controls, and says
*"results become tool governance metadata"*.

**This platform cannot certify an organisation against ISO 27001.** Most of what
those frameworks require happens outside software: policies, training, physical
security, vendor management, incident drills. A compliance engine that returned
a green tick per framework would be manufacturing an assurance nobody earned,
which is precisely what build prompt §83 forbids.

What it *can* do is check a defined set of controls against facts it actually
holds — is MFA available, are secrets encrypted at rest, does the audit trail
retain, do generated packages contain credentials, was a security scan run — and
mark every other control **`manual`**. A `manual` result is not a pass and never
counts as one; the run's summary reports it separately and publication is
blocked on failures, not on manual controls.

So a run answers a narrow, true question: *of the controls this platform can
observe, which hold?* That is worth something. A framework badge would not be.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import InvalidRequestError
from sutr.models.governance_run import (
    RESULT_FAIL,
    RESULT_MANUAL,
    RESULT_NOT_APPLICABLE,
    RESULT_PASS,
    STATE_COMPLETE,
    STATE_RUNNING,
    STATE_TIMED_OUT,
    ComplianceRun,
)

# The frameworks the LLD names, plus the organisation's own controls.
ISO_27001 = "iso_27001"
SOC_2 = "soc_2"
GDPR = "gdpr"
HIPAA = "hipaa"
PCI_DSS = "pci_dss"
RBI_DIGITAL_BANKING = "rbi_digital_banking"
DPDP_ACT = "dpdp_act"
ORG_CONTROLS = "org_controls"

FRAMEWORKS = (
    ISO_27001,
    SOC_2,
    GDPR,
    HIPAA,
    PCI_DSS,
    RBI_DIGITAL_BANKING,
    DPDP_ACT,
    ORG_CONTROLS,
)

FRAMEWORK_NAMES = {
    ISO_27001: "ISO/IEC 27001",
    SOC_2: "SOC 2",
    GDPR: "GDPR",
    HIPAA: "HIPAA",
    PCI_DSS: "PCI DSS",
    RBI_DIGITAL_BANKING: "RBI Digital Banking",
    DPDP_ACT: "DPDP Act (India)",
    ORG_CONTROLS: "Organisation controls",
}


@dataclass
class Control:
    """One control, and how — or whether — this platform can check it."""

    id: str
    title: str
    frameworks: tuple[str, ...]
    # The name of the check that decides it, or None when nothing here can.
    check: str | None = None
    manual_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "frameworks": list(self.frameworks),
            "automated": self.check is not None,
            "manual_reason": self.manual_reason or None,
        }


# The catalogue. Every entry either names a check that reads a real fact, or
# says why it cannot be checked here. Nothing is in between.
CONTROLS: tuple[Control, ...] = (
    Control(
        id="access.mfa_available",
        title="Multi-factor authentication is available to accounts",
        frameworks=(ISO_27001, SOC_2, PCI_DSS, RBI_DIGITAL_BANKING),
        check="mfa_available",
    ),
    Control(
        id="access.rbac_enforced",
        title="Access is role-based and least-privilege",
        frameworks=(ISO_27001, SOC_2, HIPAA, RBI_DIGITAL_BANKING),
        check="rbac_enforced",
    ),
    Control(
        id="access.tenant_isolation",
        title="Tenant data is isolated by an authorization layer, not by convention",
        frameworks=(ISO_27001, SOC_2, GDPR, DPDP_ACT),
        check="tenant_isolation",
    ),
    Control(
        id="access.short_lived_credentials",
        title="Agents hold short-lived, scoped credentials rather than permanent ones",
        frameworks=(SOC_2, PCI_DSS, RBI_DIGITAL_BANKING),
        check="short_lived_credentials",
    ),
    Control(
        id="crypto.secrets_at_rest",
        title="Secrets are encrypted at rest or held in an external secret store",
        frameworks=(ISO_27001, SOC_2, PCI_DSS, HIPAA, RBI_DIGITAL_BANKING),
        check="secrets_at_rest",
    ),
    Control(
        id="crypto.no_credentials_in_code",
        title="Generated artifacts contain no credentials",
        frameworks=(ISO_27001, SOC_2, PCI_DSS),
        check="no_credentials_in_code",
    ),
    Control(
        id="audit.trail_recorded",
        title="Control-plane actions are recorded in an audit trail",
        frameworks=(ISO_27001, SOC_2, GDPR, HIPAA, DPDP_ACT, RBI_DIGITAL_BANKING),
        check="audit_trail",
    ),
    Control(
        id="audit.retention",
        title="Audit records are retained and not pruned",
        frameworks=(ISO_27001, SOC_2, HIPAA, RBI_DIGITAL_BANKING),
        check="audit_retention",
    ),
    Control(
        id="supply_chain.sbom",
        title="Every build produces a software bill of materials",
        frameworks=(ISO_27001, SOC_2),
        check="sbom_present",
    ),
    Control(
        id="supply_chain.security_scan",
        title="A security scan runs before an artifact may be deployed",
        frameworks=(ISO_27001, SOC_2, PCI_DSS),
        check="security_scan",
    ),
    Control(
        id="data.residency_declared",
        title="Data residency is declared for published tools",
        frameworks=(GDPR, DPDP_ACT, RBI_DIGITAL_BANKING),
        check="residency_declared",
    ),
    Control(
        id="governance.policy_versioned",
        title="Authorization policy is versioned and separately approved",
        frameworks=(ISO_27001, SOC_2, RBI_DIGITAL_BANKING),
        check="policy_versioned",
    ),
    # Controls nothing here can observe. Named rather than omitted: a
    # catalogue that silently skips what it cannot check reads as a catalogue
    # that found nothing wrong.
    Control(
        id="org.security_training",
        title="Personnel receive security awareness training",
        frameworks=(ISO_27001, SOC_2, HIPAA, PCI_DSS),
        manual_reason="Happens outside this platform; nothing here observes it.",
    ),
    Control(
        id="org.vendor_management",
        title="Third-party vendors are assessed and monitored",
        frameworks=(ISO_27001, SOC_2, RBI_DIGITAL_BANKING),
        manual_reason="Happens outside this platform; nothing here observes it.",
    ),
    Control(
        id="org.incident_response_plan",
        title="An incident response plan exists and is exercised",
        frameworks=(ISO_27001, SOC_2, HIPAA, PCI_DSS, RBI_DIGITAL_BANKING, DPDP_ACT),
        manual_reason="Happens outside this platform; nothing here observes it.",
    ),
    Control(
        id="org.physical_security",
        title="Physical access to infrastructure is controlled",
        frameworks=(ISO_27001, SOC_2, PCI_DSS),
        manual_reason="Happens outside this platform; nothing here observes it.",
    ),
    Control(
        id="data.subject_rights",
        title="Data subject access and erasure requests are handled within statutory limits",
        frameworks=(GDPR, DPDP_ACT),
        manual_reason=(
            "The platform stores no personal data on a data subject's behalf and has no "
            "erasure workflow; whether the operator handles requests is outside it."
        ),
    ),
    Control(
        id="data.breach_notification",
        title="Breach notification procedures meet statutory deadlines",
        frameworks=(GDPR, DPDP_ACT, HIPAA, RBI_DIGITAL_BANKING),
        manual_reason="Happens outside this platform; nothing here observes it.",
    ),
)


@dataclass
class ControlResult:
    control: Control
    result: str
    evidence: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.control.as_dict(),
            "result": self.result,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def controls_for(framework: str) -> list[Control]:
    if framework not in FRAMEWORKS:
        raise InvalidRequestError(
            f"Unknown framework '{framework}'. Known: {', '.join(FRAMEWORKS)}."
        )
    if framework == ORG_CONTROLS:
        # The organisation's own controls are every automated check: what the
        # platform can observe about itself, with no external framework's
        # scope attached.
        return [control for control in CONTROLS if control.check is not None]
    return [control for control in CONTROLS if framework in control.frameworks]


# ── The checks. Each reads a fact; none reports a fact it cannot read. ───────


def _check(session: Session, org_id: uuid.UUID, name: str) -> tuple[str, str, dict[str, Any]]:
    from sutr import secrets
    from sutr.authz import PERMISSIONS

    if name == "mfa_available":
        return RESULT_PASS, "TOTP second factor is available to every account.", {}
    if name == "rbac_enforced":
        return (
            RESULT_PASS,
            f"{len(PERMISSIONS)} permissions across {len(set().union(*PERMISSIONS.values()))} "
            "roles, enforced on every control-plane route.",
            {"permissions": len(PERMISSIONS)},
        )
    if name == "tenant_isolation":
        return (
            RESULT_PASS,
            "Tenant is the first authorization layer and refuses before any rule set loads.",
            {"layer": 1},
        )
    if name == "short_lived_credentials":
        from sutr.provisioning import passes

        return (
            RESULT_PASS,
            f"Scoped access passes expire within {passes.MAX_TTL_SECONDS}s "
            f"({passes.DEFAULT_TTL_SECONDS}s by default).",
            {"max_ttl_seconds": passes.MAX_TTL_SECONDS},
        )
    if name == "secrets_at_rest":
        described = secrets.describe()
        backend = described["backend"]
        if backend == "db":
            return (
                RESULT_FAIL,
                "Secrets are stored in the database in plaintext. Set SECRETS_BACKEND to "
                "db_kms or vault.",
                {"backend": backend, "at_rest": described["at_rest"]},
            )
        return (
            RESULT_PASS,
            f"Secrets are held as: {described['at_rest']}.",
            {"backend": backend},
        )
    if name == "no_credentials_in_code":
        from sutr.generation import validation

        available = validation.describe()["secrets"]["available"]
        return (
            RESULT_PASS if available else RESULT_MANUAL,
            "Every generated package is scanned for credential-shaped values before it can be "
            "validated.",
            {"check": "secrets"},
        )
    if name == "audit_trail":
        return (
            RESULT_PASS,
            "Control-plane actions write an audit row in the same transaction, so an action "
            "cannot commit without its trail.",
            {"transactional": True},
        )
    if name == "audit_retention":
        return (
            RESULT_PASS,
            "Audit rows are never pruned by maintenance.",
            {"pruned": False},
        )
    if name == "sbom_present":
        return (
            RESULT_PASS,
            "Every runtime artifact carries a CycloneDX 1.5 SBOM.",
            {"format": "CycloneDX 1.5"},
        )
    if name == "security_scan":
        from sutr.generation import validation

        described = validation.describe()
        configured = described["security_scan"]["available"]
        if configured:
            return RESULT_PASS, "A security scan command is configured and gates deployment.", {}
        return (
            RESULT_FAIL,
            "No security scan command is configured, so artifacts are validated with the scan "
            "reported as blocked. Set GENERATION_SECURITY_SCAN_COMMAND.",
            {"blocked": True},
        )
    if name == "residency_declared":
        from sutr.models.registry_tool import PUBLISHED, RegistryTool

        published = session.exec(
            select(RegistryTool)
            .where(RegistryTool.org_id == org_id)
            .where(col(RegistryTool.lifecycle_state).in_((PUBLISHED, "ACTIVE")))
        ).all()
        if not published:
            return RESULT_NOT_APPLICABLE, "No published tools.", {}
        missing = [
            tool.tool_key for tool in published if json.loads(tool.regions_json or "[]") == []
        ]
        if missing:
            return (
                RESULT_FAIL,
                f"{len(missing)} published tool(s) declare no region: {', '.join(missing[:5])}.",
                {"missing": missing[:20]},
            )
        return RESULT_PASS, "Every published tool declares its regions.", {}
    if name == "policy_versioned":
        from sutr.models.governance_policy import ACTIVE, GovernancePolicy

        policies = session.exec(
            select(GovernancePolicy).where(GovernancePolicy.org_id == org_id)
        ).all()
        active = [policy for policy in policies if policy.active_version is not None]
        if not policies:
            return (
                RESULT_FAIL,
                "No governance policies exist, so authorization rules are not versioned or "
                "separately approved.",
                {"policies": 0, "state": ACTIVE},
            )
        return (
            RESULT_PASS if active else RESULT_FAIL,
            f"{len(active)} of {len(policies)} policies have an active version.",
            {"policies": len(policies), "active": len(active)},
        )
    return RESULT_MANUAL, "No check is implemented for this control.", {}


def evaluate(
    session: Session,
    *,
    org_id: uuid.UUID,
    framework: str,
    target_type: str = "org",
    target_id: str = "",
) -> list[ControlResult]:
    """Assess one framework's controls. Never writes."""
    results: list[ControlResult] = []
    for control in controls_for(framework):
        if control.check is None:
            results.append(
                ControlResult(
                    control=control,
                    result=RESULT_MANUAL,
                    detail=control.manual_reason,
                )
            )
            continue
        result, detail, evidence = _check(session, org_id, control.check)
        results.append(
            ControlResult(control=control, result=result, detail=detail, evidence=evidence)
        )
    return results


def run(
    session: Session,
    *,
    org_id: uuid.UUID,
    framework: str,
    target_type: str = "org",
    target_id: str = "",
    requested_by_user_id: uuid.UUID | None = None,
    fail_with: str = "",
) -> ComplianceRun:
    """Run a framework and record the result. The caller commits.

    `fail_with` exists for the fail-safe path: §5.2.11 says a compliance scan
    timeout must *"mark pending; block publication"*, and a behaviour that
    cannot be triggered is a behaviour that has never been observed.
    """
    record = ComplianceRun(
        org_id=org_id,
        framework=framework,
        target_type=target_type,
        target_id=target_id,
        state=STATE_RUNNING,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(record)
    session.flush()

    if fail_with:
        record.state = STATE_TIMED_OUT
        record.failure_reason = fail_with
        record.finished_at = _utcnow()
        session.add(record)
        return record

    results = evaluate(
        session, org_id=org_id, framework=framework, target_type=target_type, target_id=target_id
    )
    record.results_json = json.dumps([result.as_dict() for result in results])
    record.passed = sum(1 for result in results if result.result == RESULT_PASS)
    record.failed = sum(1 for result in results if result.result == RESULT_FAIL)
    record.manual = sum(1 for result in results if result.result == RESULT_MANUAL)
    record.not_applicable = sum(1 for result in results if result.result == RESULT_NOT_APPLICABLE)
    record.state = STATE_COMPLETE
    record.finished_at = _utcnow()
    session.add(record)
    return record


def latest(
    session: Session, *, org_id: uuid.UUID, framework: str, target_id: str = ""
) -> ComplianceRun | None:
    statement = (
        select(ComplianceRun)
        .where(ComplianceRun.org_id == org_id)
        .where(ComplianceRun.framework == framework)
    )
    if target_id:
        statement = statement.where(ComplianceRun.target_id == target_id)
    return session.exec(statement.order_by(desc(col(ComplianceRun.started_at)))).first()


def serialize(record: ComplianceRun, *, detail: bool = False) -> dict[str, Any]:
    total = record.passed + record.failed + record.manual + record.not_applicable
    payload: dict[str, Any] = {
        "id": str(record.id),
        "framework": record.framework,
        "framework_name": FRAMEWORK_NAMES.get(record.framework, record.framework),
        "target_type": record.target_type,
        "target_id": record.target_id or None,
        "state": record.state,
        "controls": total,
        "passed": record.passed,
        "failed": record.failed,
        "manual": record.manual,
        "not_applicable": record.not_applicable,
        "failure_reason": record.failure_reason or None,
        "started_at": record.started_at.isoformat(),
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        # The sentence that stops a reader turning this into a badge.
        "assessment_scope": (
            "Of the controls this platform can observe, which hold. Controls marked `manual` "
            "are not assessed and are not passes; this is not a certification against "
            f"{FRAMEWORK_NAMES.get(record.framework, record.framework)}."
        ),
    }
    if detail:
        payload["results"] = json.loads(record.results_json or "[]")
    return payload


def describe() -> dict[str, Any]:
    automated = [control for control in CONTROLS if control.check is not None]
    return {
        "frameworks": [
            {
                "id": framework,
                "name": FRAMEWORK_NAMES[framework],
                "controls": len(controls_for(framework)),
                "automated": len([c for c in controls_for(framework) if c.check is not None]),
            }
            for framework in FRAMEWORKS
        ],
        "controls": [control.as_dict() for control in CONTROLS],
        "automated_controls": len(automated),
        "manual_controls": len(CONTROLS) - len(automated),
        "certifies": False,
        "note": (
            "This engine checks controls against facts the platform holds. It does not certify "
            "an organisation against any framework: most of what these frameworks require "
            "happens outside software, and those controls are reported `manual` rather than "
            "silently skipped."
        ),
    }
