"""Registration, versions, and the governance gate."""

import json
import uuid

import pytest
from sqlmodel import select

from sutr.common.errors import ConflictError, ForbiddenError, InvalidRequestError
from sutr.models.registry_change_request import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    RegistryChangeRequest,
)
from sutr.models.registry_pricing import RegistryPricing
from sutr.models.registry_tool import RegistryTool
from sutr.registry import lifecycle, pricing, service, versions

# ── Registration ─────────────────────────────────────────────────────────────


def test_a_registered_tool_starts_private_in_draft(registered_tool):
    assert registered_tool.lifecycle_state == "DRAFT"
    assert registered_tool.visibility == "private"
    assert registered_tool.current_version == 0
    assert registered_tool.published_version is None


def test_a_tool_key_is_unique_within_a_tenant(session, test_org, registered_tool):
    with pytest.raises(ConflictError, match="already registered"):
        service.register(session, org_id=test_org.id, tool_key="refunds-api", name="Again")


def test_the_same_key_in_another_tenant_is_a_different_tool(session, other_org, registered_tool):
    tool = service.register(session, org_id=other_org.id, tool_key="refunds-api", name="Theirs")
    session.commit()
    assert tool.id != registered_tool.id


@pytest.mark.parametrize("key", ["ab", "A-Bad-Key", "trailing-", "has spaces", "x" * 80])
def test_a_key_that_cannot_be_an_address_is_refused(session, test_org, key):
    with pytest.raises(InvalidRequestError, match="tool key"):
        service.register(session, org_id=test_org.id, tool_key=key, name="X")


def test_editing_metadata_reports_what_changed(session, registered_tool):
    changed = service.update(
        session, registered_tool, {"name": "Refunds", "tags": ["refunds", "payments"]}
    )
    session.commit()
    # Tags were already those two, in the other order; sets, so no change.
    assert changed == ["name"]
    assert registered_tool.name == "Refunds"


def test_visibility_and_pricing_are_not_free_form_edits(session, registered_tool):
    with pytest.raises(InvalidRequestError, match="change request"):
        service.update(session, registered_tool, {"visibility": "public"})


# ── Versions ─────────────────────────────────────────────────────────────────


def test_versions_are_monotonic_and_immutable(session, registered_tool):
    first = versions.create(session, tool=registered_tool, notes="hand-cut v1")
    second = versions.create(session, tool=registered_tool, notes="hand-cut v2")
    session.commit()
    assert (first.version, second.version) == (1, 2)
    assert registered_tool.current_version == 2
    # Both are still there. Old versions are kept for rollback (LLD §3.7).
    assert [v.version for v in versions.list_for_tool(session, registered_tool.id)] == [2, 1]


def test_a_version_with_no_artifact_needs_notes(session, registered_tool):
    with pytest.raises(InvalidRequestError, match="notes"):
        versions.create(session, tool=registered_tool)


def test_a_version_carries_the_artifacts_build_hash_sbom_and_manifest(
    session, registered_tool, validated_artifact
):
    version = versions.create(session, tool=registered_tool, artifact=validated_artifact)
    session.commit()
    assert version.build_hash == validated_artifact.build_hash
    assert json.loads(version.sbom_json)["bomFormat"] == "CycloneDX"
    manifest = json.loads(version.deployment_manifest_json)
    assert manifest["package_sha256"] == validated_artifact.package_sha256
    assert manifest["transports"]


def test_an_unvalidated_artifact_cannot_become_a_version(
    session, registered_tool, rejected_artifact
):
    """A version is what a rollback restores."""
    with pytest.raises(ConflictError, match="did not pass validation"):
        versions.create(session, tool=registered_tool, artifact=rejected_artifact)


def test_the_same_artifact_cannot_be_cut_twice(session, registered_tool, validated_artifact):
    versions.create(session, tool=registered_tool, artifact=validated_artifact)
    session.commit()
    with pytest.raises(ConflictError, match="already a version"):
        versions.create(session, tool=registered_tool, artifact=validated_artifact)


# ── The governance gate ──────────────────────────────────────────────────────


def test_an_ungated_transition_applies_immediately(session, registered_tool):
    outcome = service.transition(session, registered_tool, "API_UPLOADED")
    session.commit()
    assert outcome.applied is True
    assert registered_tool.lifecycle_state == "API_UPLOADED"


def test_a_gated_transition_changes_nothing_until_it_is_decided(session, registered_tool):
    for target in (
        "API_UPLOADED",
        "TRANSLATING",
        "IR_READY",
        "METADATA_READY",
        "GENERATING_MCP",
        "VALIDATING",
        "DEPLOYING",
        "DEPLOYED",
    ):
        service.transition(session, registered_tool, target)
    outcome = service.transition(session, registered_tool, "UNDER_REVIEW", reason="ready")
    session.commit()

    assert outcome.applied is False
    assert registered_tool.lifecycle_state == "DEPLOYED"
    assert outcome.change_request.status == STATUS_PENDING
    assert json.loads(outcome.change_request.requested_json) == {"lifecycle_state": "UNDER_REVIEW"}
    assert json.loads(outcome.change_request.current_json) == {"lifecycle_state": "DEPLOYED"}


def test_approving_a_gated_transition_applies_it(session, registered_tool, second_user):
    for target in (
        "API_UPLOADED",
        "TRANSLATING",
        "IR_READY",
        "METADATA_READY",
        "GENERATING_MCP",
        "VALIDATING",
        "DEPLOYING",
        "DEPLOYED",
    ):
        service.transition(session, registered_tool, target)
    outcome = service.transition(session, registered_tool, "UNDER_REVIEW")
    service.decide(session, outcome.change_request, approve=True, decided_by_user_id=second_user.id)
    session.commit()
    assert registered_tool.lifecycle_state == "UNDER_REVIEW"


def test_rejecting_a_gated_transition_leaves_the_tool_where_it_was(
    session, registered_tool, second_user
):
    for target in (
        "API_UPLOADED",
        "TRANSLATING",
        "IR_READY",
        "METADATA_READY",
        "GENERATING_MCP",
        "VALIDATING",
        "DEPLOYING",
        "DEPLOYED",
    ):
        service.transition(session, registered_tool, target)
    outcome = service.transition(session, registered_tool, "UNDER_REVIEW")
    decided = service.decide(
        session,
        outcome.change_request,
        approve=False,
        decided_by_user_id=second_user.id,
        note="needs docs",
    )
    session.commit()
    assert decided.status == STATUS_REJECTED
    assert registered_tool.lifecycle_state == "DEPLOYED"


def test_two_pending_changes_of_the_same_kind_would_race(session, registered_tool):
    service.request_visibility(session, registered_tool, visibility="public")
    session.commit()
    with pytest.raises(ConflictError, match="already pending"):
        service.request_visibility(session, registered_tool, visibility="organization")


def test_a_requester_cannot_decide_their_own_change_when_someone_else_could(
    session, test_user, second_user, registered_tool
):
    request = service.request_visibility(
        session, registered_tool, visibility="public", requested_by_user_id=test_user.id
    )
    session.commit()
    with pytest.raises(ForbiddenError, match="somebody else"):
        service.decide(session, request, approve=True, decided_by_user_id=test_user.id)


def test_a_sole_admin_can_decide_their_own_change(session, test_user, registered_tool):
    """Four eyes where four eyes are possible. A solo install is not a deadlock."""
    request = service.request_visibility(
        session, registered_tool, visibility="public", requested_by_user_id=test_user.id
    )
    decided = service.decide(session, request, approve=True, decided_by_user_id=test_user.id)
    session.commit()
    assert decided.status == STATUS_APPROVED
    assert registered_tool.visibility == "public"
    # And it is visible as a self-decision rather than implying two people.
    assert service.serialize_change_request(decided)["self_decided"] is True


def test_a_withdrawn_request_cannot_be_decided(session, registered_tool, second_user):
    request = service.request_visibility(session, registered_tool, visibility="public")
    service.withdraw(session, request, user_id=second_user.id)
    session.commit()
    assert request.status == STATUS_WITHDRAWN
    with pytest.raises(ConflictError, match="already withdrawn"):
        service.decide(session, request, approve=True, decided_by_user_id=second_user.id)


# ── Pricing ──────────────────────────────────────────────────────────────────


def test_a_price_change_is_gated_and_validated_when_requested(session, registered_tool):
    with pytest.raises(InvalidRequestError, match="needs an amount"):
        service.request_pricing(
            session, registered_tool, price={"model": "per_call", "amount_micros": 0}
        )


def test_approving_a_price_supersedes_the_old_one_rather_than_editing_it(
    session, registered_tool, second_user
):
    first = service.request_pricing(
        session,
        registered_tool,
        price={"model": "per_call", "amount_micros": 2000, "unit": "call"},
    )
    service.decide(session, first, approve=True, decided_by_user_id=second_user.id)
    session.commit()

    second = service.request_pricing(
        session,
        registered_tool,
        price={"model": "per_call", "amount_micros": 5000, "unit": "call"},
    )
    service.decide(session, second, approve=True, decided_by_user_id=second_user.id)
    session.commit()

    rows = session.exec(select(RegistryPricing)).all()
    assert len(rows) == 2, "the old price is history, not an edit"
    live = pricing.current(session, registered_tool.id)
    assert live.amount_micros == 5000
    superseded = [row for row in rows if row.superseded_at is not None]
    assert len(superseded) == 1 and superseded[0].amount_micros == 2000


def test_a_rejected_price_change_does_not_move_the_price(session, registered_tool, second_user):
    request = service.request_pricing(
        session, registered_tool, price={"model": "per_call", "amount_micros": 9000}
    )
    service.decide(session, request, approve=False, decided_by_user_id=second_user.id)
    session.commit()
    assert pricing.current(session, registered_tool.id) is None
    assert session.exec(select(RegistryPricing)).all() == []


def test_an_unpriced_tool_is_null_not_free():
    """ "Nobody set a price" and "the provider chose free" are different facts."""
    assert pricing.serialize(None) is None


def test_a_price_says_it_is_not_billed_by_this_platform(session, registered_tool, second_user):
    request = service.request_pricing(
        session,
        registered_tool,
        price={"model": "subscription", "amount_micros": 10_000_000, "unit": "month"},
    )
    service.decide(session, request, approve=True, decided_by_user_id=second_user.id)
    session.commit()
    payload = pricing.serialize(pricing.current(session, registered_tool.id))
    assert payload["billed_by_this_platform"] is False
    assert payload["display"] == "10 USD per month"


# ── Retirement ───────────────────────────────────────────────────────────────


def test_deprecation_needs_a_note(session, test_org, registered_tool, second_user):
    from .conftest import publish

    publish(session, registered_tool, decided_by=second_user.id)
    with pytest.raises(InvalidRequestError, match="needs a note"):
        service.deprecate(session, registered_tool, note="  ")


def test_deprecating_records_the_note_and_the_time(session, registered_tool, second_user):
    from .conftest import publish

    publish(session, registered_tool, decided_by=second_user.id)
    service.deprecate(session, registered_tool, note="Use refunds-api-v2 instead.")
    session.commit()
    assert registered_tool.lifecycle_state == "DEPRECATED"
    assert registered_tool.deprecated_at is not None
    assert "refunds-api-v2" in registered_tool.deprecation_note


# ── Tenancy ──────────────────────────────────────────────────────────────────


def test_another_tenants_tool_is_not_found(session, other_org, registered_tool):
    assert service.get(session, registered_tool.id, other_org.id) is None
    assert service.list_tools(session, org_id=other_org.id) == []


def test_a_missing_tool_is_not_found(session, test_org):
    assert service.get(session, uuid.uuid4(), test_org.id) is None


def test_publishing_marks_the_version_and_the_pointer(
    session, registered_tool, second_user, validated_artifact
):
    from .conftest import publish

    versions.create(session, tool=registered_tool, artifact=validated_artifact)
    session.commit()
    publish(session, registered_tool, decided_by=second_user.id)

    assert registered_tool.lifecycle_state == "PUBLISHED"
    assert registered_tool.published_version == 1
    assert registered_tool.published_at is not None
    assert versions.get(session, registered_tool.id, 1).was_published is True


def test_serialization_says_regions_and_compliance_are_provider_claims(session, registered_tool):
    payload = service.serialize(session, registered_tool, detail=True)
    assert payload["declared_by_provider"] == ["regions", "compliance"]
    assert payload["compliance"] == ["SOC2"]
    assert payload["progress"]["step"] == 1
    assert lifecycle.PIPELINE[0] == "DRAFT"
    assert payload["allowed_transitions"] == [{"to": "API_UPLOADED", "requires_approval": False}]


def test_every_registry_tool_row_belongs_to_one_org(session, test_org, registered_tool):
    rows = session.exec(select(RegistryTool)).all()
    assert {row.org_id for row in rows} == {test_org.id}
    assert session.exec(select(RegistryChangeRequest)).all() == []
