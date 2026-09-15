"""The six-stage approval workflow and time-boxed exceptions."""

from datetime import datetime, timedelta, timezone

import pytest

from sutr.common.errors import ConflictError, ForbiddenError, InvalidRequestError
from sutr.governance import compliance, exceptions, review, risk
from sutr.models.governance_review import STAGES


def _stage(record, name):
    return next(s for s in review.stage_results(record) if s["stage"] == name)


# ── The workflow ─────────────────────────────────────────────────────────────


def test_the_stages_are_the_llds_six_in_order():
    assert STAGES == (
        "upload",
        "automated_validation",
        "security_scan",
        "compliance_review",
        "manual_approval",
        "publication",
    )


def test_a_tool_with_no_version_blocks_at_upload(session, test_org, tool, test_user):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    assert record.state == "blocked"
    assert record.current_stage == "upload"
    assert "no version" in record.blocked_reason
    # And the stage says where the outcome came from.
    assert _stage(record, "upload")["source"] == "registry_version"


def test_every_stage_names_its_evidence_source(session, test_org, tool, test_user):
    from sutr.registry import versions

    versions.create(session, tool=tool, notes="v1")
    session.commit()
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    for stage in review.stage_results(record):
        assert stage["source"], f"{stage['stage']} asserted an outcome with no source"


def test_a_version_without_an_artifact_blocks_at_validation(session, test_org, tool, test_user):
    from sutr.registry import versions

    versions.create(session, tool=tool, notes="hand-cut")
    session.commit()
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    assert record.state == "blocked"
    assert record.current_stage == "automated_validation"
    assert "no validation report" in record.blocked_reason


def test_a_missing_compliance_run_leaves_the_workflow_pending(
    session, test_org, tool, test_user, validated_version
):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    assert record.state == "open"
    assert record.current_stage == "compliance_review"
    assert "No org_controls compliance run" in _stage(record, "compliance_review")["detail"]


def test_a_timed_out_compliance_run_blocks_publication(
    session, test_org, tool, test_user, validated_version
):
    """§5.2.11, end to end: the timeout reaches the workflow and stops it."""
    compliance.run(
        session,
        org_id=test_org.id,
        framework=compliance.ORG_CONTROLS,
        fail_with="the scanner did not answer",
    )
    session.commit()
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    assert record.state == "blocked"
    assert record.current_stage == "compliance_review"
    assert "timed out" in record.blocked_reason


def test_a_failing_compliance_run_blocks(
    session, test_org, tool, test_user, validated_version, monkeypatch
):
    from sutr.config import settings

    monkeypatch.setattr(settings, "secrets_backend", "db")
    compliance.run(session, org_id=test_org.id, framework=compliance.ORG_CONTROLS)
    session.commit()
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    assert record.state == "blocked"
    assert "compliance control(s) failed" in record.blocked_reason


def test_a_clean_run_reaches_manual_approval_with_a_risk_score(
    session, test_org, tool, test_user, validated_version, clean_compliance
):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    assert record.state == "open"
    assert record.current_stage == "manual_approval"
    assert record.risk_score is not None
    # And the compliance stage reports manual controls without counting them.
    detail = _stage(record, "compliance_review")["detail"]
    assert "not counted as passes" in detail


def test_the_requester_cannot_decide_their_own_review(
    session, test_org, tool, test_user, validated_version, clean_compliance
):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    with pytest.raises(ForbiddenError, match="somebody else"):
        review.decide(session, record, approve=True, decided_by_user_id=test_user.id)


def test_a_second_admin_can_approve(
    session, test_org, tool, test_user, second_admin, validated_version, clean_compliance
):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    review.decide(session, record, approve=True, decided_by_user_id=second_admin.id, note="fine")
    session.commit()
    assert record.state == "approved"
    assert record.current_stage == "publication"
    assert record.auto_approved is False


def test_a_blocked_review_cannot_be_decided(session, test_org, tool, test_user, second_admin):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    with pytest.raises(ConflictError, match="blocked"):
        review.decide(session, record, approve=True, decided_by_user_id=second_admin.id)


def test_unknown_risk_never_auto_approves(
    session, test_org, tool, test_user, validated_version, clean_compliance
):
    """ "Nothing could be measured" is not "low risk"."""
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    record.risk_score = None
    session.add(record)
    session.commit()
    allowed, reason = review.can_auto_approve(record)
    assert allowed is False
    assert "unknown risk is not low risk" in reason
    with pytest.raises(ConflictError):
        review.auto_approve(session, record)


def test_a_low_risk_tool_can_auto_approve_and_the_record_says_it_did(
    session, test_org, tool, test_user, validated_version, clean_compliance
):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    record.risk_score = risk.AUTO_APPROVE_BELOW - 1
    session.add(record)
    session.commit()
    review.auto_approve(session, record)
    session.commit()
    assert record.state == "approved"
    assert record.auto_approved is True
    assert "below the auto-approval threshold" in record.decision_note


def test_a_high_risk_tool_cannot_auto_approve(
    session, test_org, tool, test_user, validated_version, clean_compliance
):
    record = review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    record.risk_score = risk.AUTO_APPROVE_BELOW + 10
    session.add(record)
    session.commit()
    allowed, reason = review.can_auto_approve(record)
    assert allowed is False
    assert "at or above the auto-approval threshold" in reason


def test_two_open_reviews_of_one_tool_are_refused(session, test_org, tool, test_user):
    review.open_review(session, tool=tool, requested_by_user_id=test_user.id)
    session.commit()
    with pytest.raises(ConflictError, match="already open"):
        review.open_review(session, tool=tool, requested_by_user_id=test_user.id)


# ── Exceptions ───────────────────────────────────────────────────────────────


def _request(session, test_org, test_user, **overrides):
    body = {
        "org_id": test_org.id,
        "violation": "secrets are stored in plaintext",
        "justification": "Vault lands next sprint; this blocks the pilot.",
        "control": "crypto.secrets_at_rest",
        "scope_type": "org",
        "scope_id": str(test_org.id),
        "requested_by_user_id": test_user.id,
    }
    body.update(overrides)
    return exceptions.request(session, **body)


def test_an_exception_always_expires(session, test_org, test_user):
    record = _request(session, test_org, test_user)
    session.commit()
    assert record.expires_at is not None
    assert record.revalidate_at < record.expires_at
    assert exceptions.describe()["all_expire"] is True


def test_an_exception_longer_than_the_ceiling_is_refused(session, test_org, test_user):
    with pytest.raises(InvalidRequestError, match="policy change in disguise"):
        _request(session, test_org, test_user, days=exceptions.MAX_DAYS + 1)


def test_an_exception_with_no_violation_or_justification_is_refused(session, test_org, test_user):
    with pytest.raises(InvalidRequestError, match="violation"):
        _request(session, test_org, test_user, violation="  ")
    with pytest.raises(InvalidRequestError, match="justification"):
        _request(session, test_org, test_user, justification="  ")


def test_approval_requires_a_risk_assessment_first(session, test_org, test_user, second_admin):
    """The LLD's flow puts assessment before decision, so approval enforces it."""
    record = _request(session, test_org, test_user)
    session.commit()
    with pytest.raises(ConflictError, match="no risk assessment"):
        exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)

    exceptions.assess(session, record, assessment={"score": 40, "note": "bounded blast radius"})
    exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)
    session.commit()
    assert record.state == "approved"


def test_a_requester_cannot_decide_their_own_exception(session, test_org, test_user):
    record = _request(session, test_org, test_user)
    exceptions.assess(session, record, assessment={"score": 1})
    session.commit()
    with pytest.raises(ForbiddenError, match="control removed"):
        exceptions.decide(session, record, approve=True, decided_by_user_id=test_user.id)


def test_a_rejection_needs_no_assessment(session, test_org, test_user, second_admin):
    record = _request(session, test_org, test_user)
    exceptions.decide(session, record, approve=False, decided_by_user_id=second_admin.id)
    session.commit()
    assert record.state == "rejected"


def test_an_expired_exception_is_not_honoured_even_before_the_sweep(
    session, test_org, test_user, second_admin
):
    record = _request(session, test_org, test_user)
    exceptions.assess(session, record, assessment={"score": 1})
    exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)
    session.commit()
    assert exceptions.active_for(session, org_id=test_org.id, scope_id=str(test_org.id)) is not None

    record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.add(record)
    session.commit()
    # Still `approved` in the row, because nothing has swept — and still not
    # honoured, because the decision path checks the date itself.
    assert record.state == "approved"
    assert exceptions.active_for(session, org_id=test_org.id, scope_id=str(test_org.id)) is None


def test_the_sweep_expires_what_is_due(session, test_org, test_user, second_admin):
    record = _request(session, test_org, test_user)
    exceptions.assess(session, record, assessment={"score": 1})
    exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)
    record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.add(record)
    session.commit()
    assert exceptions.expire_due(session) == 1
    session.commit()
    assert record.state == "expired"


def test_revalidation_extends_within_the_same_ceiling(session, test_org, test_user, second_admin):
    record = _request(session, test_org, test_user, days=10)
    exceptions.assess(session, record, assessment={"score": 1})
    exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)
    session.commit()
    exceptions.revalidate(session, record, days=30, note="still needed")
    session.commit()
    assert record.state == "approved"
    with pytest.raises(InvalidRequestError):
        exceptions.revalidate(session, record, days=exceptions.MAX_DAYS + 1)


def test_revalidation_falls_due_before_expiry(session, test_org, test_user, second_admin):
    record = _request(session, test_org, test_user, days=90)
    exceptions.assess(session, record, assessment={"score": 1})
    exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)
    session.commit()
    later = datetime.now(timezone.utc) + timedelta(days=46)
    due = exceptions.due_for_revalidation(session, org_id=test_org.id, now=later)
    assert [row.id for row in due] == [record.id]


def test_an_approved_exception_becomes_a_measured_violation(
    session, test_org, tool, test_user, second_admin
):
    """An exception is a documented deviation, so it registers as one.

    The assertion is about the *component*, not the aggregate. A risk score is
    risk out of what was measured, so a component becoming measurable changes
    the denominator — and a low-risk one can pull the normalised number down
    even as coverage improves. `risk.describe()["comparability"]` says so, and
    this test is where that would otherwise be discovered by surprise.
    """
    before = risk.compute(session, tool)
    assert next(c for c in before.components if c.name == "violations").available is False

    record = _request(
        session, test_org, test_user, scope_type="registry_tool", scope_id=str(tool.id)
    )
    exceptions.assess(session, record, assessment={"score": 1})
    exceptions.decide(session, record, approve=True, decided_by_user_id=second_admin.id)
    session.commit()

    after = risk.compute(session, tool)
    violations = next(c for c in after.components if c.name == "violations")
    assert violations.available is True
    assert violations.value > 0, "an approved exception carries risk"
    assert violations.detail["open_exceptions"] == 1
    assert after.coverage > before.coverage
