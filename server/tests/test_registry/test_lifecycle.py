"""The tool lifecycle state machine (LLD §3.1's shared vocabulary)."""

import pytest

from sutr.registry import lifecycle


def test_the_pipeline_is_the_lld_fourteen_states_in_order():
    assert lifecycle.PIPELINE == (
        "DRAFT",
        "API_UPLOADED",
        "TRANSLATING",
        "IR_READY",
        "DOC_PROCESSING",
        "METADATA_READY",
        "GENERATING_MCP",
        "VALIDATING",
        "DEPLOYING",
        "DEPLOYED",
        "UNDER_REVIEW",
        "APPROVED",
        "PUBLISHED",
        "ACTIVE",
    )


def test_every_failure_state_resumes_at_the_stage_that_failed():
    """LLD §4.1: failure states are resumable from the last good stage."""
    for failure, resume in lifecycle.RESUME_FROM.items():
        assert resume in lifecycle.PIPELINE
        assert resume in lifecycle.allowed_targets(failure), failure


def test_a_pipeline_failure_can_also_be_abandoned_back_to_draft():
    """A provider who decided the spec was wrong should not have to fake a fix."""
    for failure in (
        "TRANSLATION_FAILED",
        "DOCUMENTATION_FAILED",
        "GENERATION_FAILED",
        "DEPLOYMENT_FAILED",
        "REJECTED",
    ):
        assert "DRAFT" in lifecycle.allowed_targets(failure), failure


def test_a_suspended_tool_cannot_be_sent_back_to_draft():
    """It has subscribers. Restore it, deprecate it, or archive it — those are
    the three honest answers; reverting it to DRAFT would orphan them."""
    assert lifecycle.allowed_targets("SUSPENDED") == ("ARCHIVED", "DEPRECATED", "PUBLISHED")


def test_a_transition_the_machine_does_not_allow_names_both_ends():
    with pytest.raises(lifecycle.TransitionError) as excinfo:
        lifecycle.check("DRAFT", "PUBLISHED")
    message = str(excinfo.value)
    assert "DRAFT" in message and "PUBLISHED" in message
    assert "API_UPLOADED" in message, "the message says what is possible instead"


def test_moving_to_the_state_you_are_already_in_is_refused():
    with pytest.raises(lifecycle.TransitionError, match="already"):
        lifecycle.check("DRAFT", "DRAFT")


def test_an_unknown_state_is_refused():
    with pytest.raises(lifecycle.TransitionError, match="not a lifecycle state"):
        lifecycle.check("DRAFT", "SHIPPED")


def test_only_review_and_publication_are_gated():
    """A gate on every transition would be a human approving a parser."""
    gated = {(source, target) for source, target in lifecycle.GATED}
    assert gated == {("DEPLOYED", "UNDER_REVIEW"), ("APPROVED", "PUBLISHED")}
    assert lifecycle.check("DEPLOYED", "UNDER_REVIEW").gated is True
    assert lifecycle.check("TRANSLATING", "IR_READY").gated is False


def test_archived_is_terminal():
    assert lifecycle.allowed_targets("ARCHIVED") == ()
    with pytest.raises(lifecycle.TransitionError, match="terminal"):
        lifecycle.check("ARCHIVED", "PUBLISHED")


def test_a_deprecated_tool_can_be_brought_back():
    assert "PUBLISHED" in lifecycle.allowed_targets("DEPRECATED")


def test_a_published_tool_can_be_regenerated_after_a_fix():
    assert "GENERATING_MCP" in lifecycle.allowed_targets("ACTIVE")
    assert "GENERATING_MCP" in lifecycle.allowed_targets("DEPLOYED")


def test_progress_places_a_failure_at_the_stage_it_will_resume_at():
    """A tool that failed deployment is nine steps in and stuck, not zero."""
    failed = lifecycle.progress("DEPLOYMENT_FAILED")
    assert failed["step"] == lifecycle.PIPELINE.index("DEPLOYING") + 1
    assert failed["failed"] is True
    assert lifecycle.progress("DRAFT") == {
        "step": 1,
        "of": 14,
        "stage": "DRAFT",
        "failed": False,
        "retired": False,
    }


def test_every_transition_target_is_a_known_state():
    for source, targets in lifecycle.TRANSITIONS.items():
        assert lifecycle.is_known(source), source
        for target in targets:
            assert lifecycle.is_known(target), f"{source} -> {target}"


def test_describe_reports_the_whole_machine():
    described = lifecycle.describe()
    assert len(described["pipeline"]) == 14
    assert len(described["failure_states"]) == 6
    assert {"from": "APPROVED", "to": "PUBLISHED"} in described["gated_transitions"]
