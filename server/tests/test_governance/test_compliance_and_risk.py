"""The compliance and risk engines, and what they refuse to claim."""

import pytest

from sutr.config import settings
from sutr.governance import compliance, risk
from sutr.models.governance_run import (
    RESULT_FAIL,
    RESULT_MANUAL,
    RESULT_PASS,
    STATE_COMPLETE,
    STATE_TIMED_OUT,
)


def _result(results, control_id):
    return next(r for r in results if r.control.id == control_id)


# ── Compliance ───────────────────────────────────────────────────────────────


def test_the_engine_does_not_claim_to_certify():
    described = compliance.describe()
    assert described["certifies"] is False
    assert "does not certify" in described["note"]
    assert len(described["frameworks"]) == 8


def test_controls_it_cannot_check_are_manual_not_passes(session, test_org):
    results = compliance.evaluate(session, org_id=test_org.id, framework=compliance.ISO_27001)
    training = _result(results, "org.security_training")
    assert training.result == RESULT_MANUAL
    assert "outside this platform" in training.detail
    # And a manual result is never counted among the passes.
    assert training.result != RESULT_PASS


def test_the_default_install_fails_the_secrets_at_rest_control(session, test_org, monkeypatch):
    """Plaintext secrets are a fail, not a manual, because it is observable."""
    monkeypatch.setattr(settings, "secrets_backend", "db")
    results = compliance.evaluate(session, org_id=test_org.id, framework=compliance.SOC_2)
    secrets_control = _result(results, "crypto.secrets_at_rest")
    assert secrets_control.result == RESULT_FAIL
    assert "plaintext" in secrets_control.detail


def test_configuring_a_backend_passes_the_same_control(session, test_org, monkeypatch):
    monkeypatch.setattr(settings, "secrets_backend", "db_kms")
    results = compliance.evaluate(session, org_id=test_org.id, framework=compliance.SOC_2)
    assert _result(results, "crypto.secrets_at_rest").result == RESULT_PASS


def test_an_unscanned_install_fails_the_security_scan_control(session, test_org, monkeypatch):
    monkeypatch.setattr(settings, "generation_security_scan_command", "")
    results = compliance.evaluate(session, org_id=test_org.id, framework=compliance.PCI_DSS)
    scan = _result(results, "supply_chain.security_scan")
    assert scan.result == RESULT_FAIL
    assert "GENERATION_SECURITY_SCAN_COMMAND" in scan.detail


def test_a_published_tool_with_no_region_fails_the_residency_control(
    session, test_org, tool, test_user, second_admin
):
    from sutr.registry import service as registry_service

    registry_service.update(session, tool, {"regions": []})
    tool.lifecycle_state = "PUBLISHED"
    session.add(tool)
    session.commit()
    results = compliance.evaluate(session, org_id=test_org.id, framework=compliance.GDPR)
    residency = _result(results, "data.residency_declared")
    assert residency.result == RESULT_FAIL
    assert "refunds-api" in residency.detail


def test_a_run_records_every_outcome_and_says_what_it_assessed(session, test_org):
    record = compliance.run(session, org_id=test_org.id, framework=compliance.SOC_2)
    session.commit()
    assert record.state == STATE_COMPLETE
    assert record.passed + record.failed + record.manual + record.not_applicable > 0
    payload = compliance.serialize(record, detail=True)
    assert "not a certification" in payload["assessment_scope"]
    assert len(payload["results"]) == payload["controls"]


def test_a_timed_out_run_is_its_own_state(session, test_org):
    """§5.2.11: compliance scan timeout — mark pending, block publication."""
    record = compliance.run(
        session,
        org_id=test_org.id,
        framework=compliance.SOC_2,
        fail_with="the scanner did not answer in 300s",
    )
    session.commit()
    assert record.state == STATE_TIMED_OUT
    assert record.passed == 0
    assert "did not answer" in record.failure_reason


def test_an_unknown_framework_is_refused(session, test_org):
    from sutr.common.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError, match="Unknown framework"):
        compliance.evaluate(session, org_id=test_org.id, framework="pci_dss_v9")


def test_org_controls_is_every_check_the_platform_can_make():
    automated = [c for c in compliance.CONTROLS if c.check is not None]
    assert compliance.controls_for(compliance.ORG_CONTROLS) == automated


# ── Risk ─────────────────────────────────────────────────────────────────────


def test_lower_is_better_and_the_response_says_so(session, test_org, tool):
    result = risk.compute(session, tool)
    payload = result.as_dict()
    assert payload["lower_is_better"] is True
    assert "opposite of the registry's trust score" in risk.describe()["note"]


def test_the_weights_sum_to_one_hundred_over_the_llds_seven_inputs():
    assert sum(risk.WEIGHTS.values()) == 100
    assert set(risk.WEIGHTS) == {
        "scan_findings",
        "cves",
        "verification_status",
        "stability",
        "incidents",
        "violations",
        "data_sensitivity",
    }


def test_an_unmeasurable_input_abstains_rather_than_scoring_zero_risk(session, test_org, tool):
    result = risk.compute(session, tool)
    absent = [c for c in result.components if not c.available]
    assert absent, "a fresh tool cannot have measured everything"
    for component in absent:
        assert component.unavailable_reason
        assert component.value is None
    assert result.coverage < 1.0
    assert "Not measured:" in result.explain()


def test_a_declared_sensitive_domain_raises_the_score(session, test_org, tool):
    from sutr.registry import service as registry_service

    before = risk.compute(session, tool).score
    registry_service.update(session, tool, {"compliance": ["HIPAA"]})
    session.commit()
    after = risk.compute(session, tool)
    assert after.score > before
    sensitivity = next(c for c in after.components if c.name == "data_sensitivity")
    assert sensitivity.detail["level"] == "health"
    assert "nothing inspects payloads" in sensitivity.detail["source"]


def test_a_failed_recalculation_keeps_the_previous_score_and_flags_it(session, test_org, tool):
    """§5.2.11: risk calc failure — keep previous score, flag recalc."""
    first = risk.record(session, org_id=test_org.id, tool=tool)
    session.commit()
    original = first.score

    second = risk.record(
        session, org_id=test_org.id, tool=tool, failure="the scanner is unreachable"
    )
    session.commit()
    assert second.id == first.id, "the existing row is flagged, not replaced"
    assert second.score == original, "a score is never overwritten with nothing"
    assert second.stale is True
    assert "unreachable" in second.stale_reason


def test_a_first_ever_failure_records_the_unknown_rather_than_inventing_a_score(
    session, test_org, tool
):
    assessment = risk.record(session, org_id=test_org.id, tool=tool, failure="no scanner")
    session.commit()
    assert assessment.score is None
    assert assessment.stale is True


def test_a_low_score_is_eligible_for_auto_approval_and_none_is_not(session, test_org, tool):
    result = risk.compute(session, tool)
    assert result.as_dict()["auto_approve_threshold"] == risk.AUTO_APPROVE_BELOW
    empty = risk.Risk(score=None, components=[], coverage=0.0, unavailable_reason="nothing")
    assert empty.low is False, "unknown risk is not low risk"
