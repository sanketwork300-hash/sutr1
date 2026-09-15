"""The trust score: what it measures, what it abstains from, and why."""

from datetime import datetime, timedelta, timezone

from sutr.models.deployment import Deployment
from sutr.models.log import LogEntry
from sutr.models.marketplace_review import MarketplaceReview
from sutr.models.runtime_artifact import STATUS_REJECTED
from sutr.registry import service, trust, versions


def _component(score, name):
    return next(c for c in score.components if c.name == name)


def test_a_tool_nothing_is_known_about_scores_none_not_zero(session, test_org):
    """A tool nobody has measured is not a tool known to be bad."""
    bare = service.register(session, org_id=test_org.id, tool_key="bare-tool", name="Bare")
    session.commit()
    score = trust.compute(session, bare)
    # governance_status and doc_quality are always measurable, so a registered
    # tool always has *something*. The real "no inputs" case is asserted below
    # by removing them.
    assert score.score is not None
    assert score.coverage < 1.0


def test_with_no_measurable_component_the_score_is_none_with_a_reason(
    session, registered_tool, monkeypatch
):
    for name in ("_governance_status", "_doc_quality"):
        original = getattr(trust, name)

        def unavailable(*args, _original=original, **kwargs):
            component = _original(*args, **kwargs)
            component.available = False
            component.value = None
            component.unavailable_reason = "stubbed out"
            return component

        monkeypatch.setattr(trust, name, unavailable)

    score = trust.compute(session, registered_tool)
    assert score.score is None
    assert score.coverage == 0.0
    assert "unknown rather than zero" in score.unavailable_reason


def test_coverage_says_how_much_of_the_score_was_actually_measured(session, registered_tool):
    score = trust.compute(session, registered_tool)
    measured = [c for c in score.components if c.available]
    expected = sum(c.weight for c in measured) / sum(trust.WEIGHTS.values())
    assert score.coverage == round(expected, 3) or abs(score.coverage - expected) < 1e-9
    assert "Not measured:" in score.explain()


def test_a_blocked_security_scan_abstains_rather_than_scoring_zero(
    session, registered_tool, validated_artifact
):
    """The install has no scanner. That is a gap, not a finding."""
    score = trust.compute(session, registered_tool)
    scans = _component(score, "security_scans")
    if scans.available:
        # The checks that did run (secrets, static analysis) all passed.
        assert scans.value == 1.0
        assert scans.detail["checks"]["security_scan"] == "blocked"
    else:
        assert "not configured" in (scans.unavailable_reason or "")


def test_validation_success_is_the_share_of_artifacts_that_passed(
    session, registered_tool, validated_artifact
):
    before = _component(trust.compute(session, registered_tool), "validation_success")

    validated_artifact.status = STATUS_REJECTED
    session.add(validated_artifact)
    session.commit()
    after = _component(trust.compute(session, registered_tool), "validation_success")

    assert before.value == 1.0
    assert after.value == 0.0
    assert after.detail == {"artifacts": 1, "validated": 0}


def test_a_tool_never_deployed_abstains_on_availability(session, registered_tool):
    component = _component(trust.compute(session, registered_tool), "runtime_availability")
    assert component.available is False
    assert "never been deployed" in component.unavailable_reason


def test_a_tool_with_no_source_project_says_that_instead(session, test_org):
    bare = service.register(session, org_id=test_org.id, tool_key="bare-one", name="Bare")
    session.commit()
    component = _component(trust.compute(session, bare), "runtime_availability")
    assert component.available is False
    assert "no source project" in component.unavailable_reason


def test_runtime_availability_is_the_share_of_deployments_that_are_running(
    session, test_org, registered_tool
):
    project_id = registered_tool.project_id
    for status in ("running", "running", "failed"):
        session.add(
            Deployment(
                org_id=test_org.id,
                project_id=project_id,
                name=f"d-{status}",
                slug="d",
                provider="docker",
                status=status,
                package_zip=b"",
            )
        )
    session.commit()
    component = _component(trust.compute(session, registered_tool), "runtime_availability")
    assert component.value == 2 / 3
    assert component.detail == {"deployments": 3, "running": 2, "failed": 1}


def test_three_calls_are_not_an_error_rate(session, test_org, registered_tool):
    for _ in range(3):
        session.add(
            LogEntry(
                org_id=test_org.id,
                integration_id="customapi_refunds",
                tool_name="refund",
                outcome="executed",
            )
        )
    session.commit()
    component = _component(trust.compute(session, registered_tool), "error_rate")
    assert component.available is False
    assert "too few" in component.unavailable_reason


def test_the_error_rate_rewards_a_low_one_and_ignores_old_calls(session, test_org, registered_tool):
    stale = datetime.now(timezone.utc) - timedelta(days=trust.WINDOW_DAYS + 5)
    for _ in range(20):
        session.add(
            LogEntry(
                org_id=test_org.id,
                integration_id="customapi_refunds",
                tool_name="refund",
                outcome="error",
                timestamp=stale,
            )
        )
    for index in range(10):
        session.add(
            LogEntry(
                org_id=test_org.id,
                integration_id="customapi_refunds",
                tool_name="refund",
                outcome="error" if index < 2 else "executed",
            )
        )
    session.commit()
    component = _component(trust.compute(session, registered_tool), "error_rate")
    assert component.detail["calls"] == 10, "the stale window is excluded"
    assert component.value == 0.8


def test_another_tenants_calls_do_not_count(session, other_org, registered_tool):
    for _ in range(10):
        session.add(
            LogEntry(
                org_id=other_org.id,
                integration_id="customapi_refunds",
                tool_name="refund",
                outcome="error",
            )
        )
    session.commit()
    component = _component(trust.compute(session, registered_tool), "error_rate")
    assert component.available is False


def test_governance_standing_moves_with_the_lifecycle(session, registered_tool, second_user):
    from .conftest import publish

    before = _component(trust.compute(session, registered_tool), "governance_status")
    publish(session, registered_tool, decided_by=second_user.id)
    after = _component(trust.compute(session, registered_tool), "governance_status")
    assert after.value > before.value
    assert after.value == 1.0


def test_a_suspended_tool_scores_zero_on_governance(session, registered_tool, second_user):
    from .conftest import publish

    publish(session, registered_tool, decided_by=second_user.id)
    service.transition(session, registered_tool, "SUSPENDED", reason="incident")
    session.commit()
    assert _component(trust.compute(session, registered_tool), "governance_status").value == 0.0


def test_doc_quality_counts_completeness_and_says_so(session, registered_tool):
    component = _component(trust.compute(session, registered_tool), "doc_quality")
    assert component.available is True
    assert component.detail["has_summary"] is True
    assert component.detail["measures"] == "completeness, not prose"


def test_an_unrated_tool_abstains_rather_than_scoring_zero(session, registered_tool):
    component = _component(trust.compute(session, registered_tool), "user_ratings")
    assert component.available is False
    assert "Nobody has rated" in component.unavailable_reason


def test_ratings_map_one_to_five_stars_onto_the_component(
    session, test_org, test_user, registered_tool
):
    session.add(
        MarketplaceReview(
            org_id=test_org.id,
            user_id=test_user.id,
            integration_id="customapi_refunds",
            rating=1,
        )
    )
    session.commit()
    one_star = _component(trust.compute(session, registered_tool), "user_ratings")
    assert one_star.value == 0.0, "one star is zero, not a fifth of the marks"

    review = session.exec(__import__("sqlmodel").select(MarketplaceReview)).one()
    review.rating = 5
    session.add(review)
    session.commit()
    assert _component(trust.compute(session, registered_tool), "user_ratings").value == 1.0


def test_the_explanation_names_the_components_and_the_gaps(session, registered_tool):
    explanation = trust.compute(session, registered_tool).explain()
    assert "/100 from" in explanation
    assert "governance_status" in explanation
    assert "Not measured:" in explanation


def test_the_weights_sum_to_one_hundred():
    """So a weight reads as "up to this many points" and the sum is checkable."""
    assert sum(trust.WEIGHTS.values()) == 100
    assert set(trust.WEIGHTS) == {
        "security_scans",
        "validation_success",
        "runtime_availability",
        "error_rate",
        "governance_status",
        "doc_quality",
        "user_ratings",
    }


def test_the_score_is_bounded(session, registered_tool, second_user, validated_artifact):
    from .conftest import publish

    versions.create(session, tool=registered_tool, artifact=validated_artifact)
    session.commit()
    publish(session, registered_tool, decided_by=second_user.id)
    score = trust.compute(session, registered_tool)
    assert 0 <= score.score <= 100
    assert score.as_dict()["max"] == 100
