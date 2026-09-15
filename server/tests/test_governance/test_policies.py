"""Policy versions, the lifecycle, and separation of duties (LLD §5.2)."""

import json

import pytest

from sutr.common.errors import ConflictError, ForbiddenError, InvalidRequestError
from sutr.governance import policies
from sutr.models.governance_policy import LIFECYCLE
from sutr.provisioning import identity, pdp, rules

from .conftest import ACCESS_DOCUMENT, activate


def test_the_lifecycle_is_the_llds_seven_states_in_order():
    assert LIFECYCLE == (
        "DRAFT",
        "REVIEW",
        "APPROVED",
        "PUBLISHED",
        "ACTIVE",
        "DEPRECATED",
        "ARCHIVED",
    )


def test_a_new_policy_starts_as_a_draft_with_no_active_version(policy):
    assert policy.current_version == 1
    assert policy.active_version is None


def test_a_version_stops_being_editable_once_it_leaves_draft(
    session, policy, test_user, second_admin
):
    version = policies.get_version(session, policy.id, 1)
    policies.edit(session, policy, version, document=ACCESS_DOCUMENT, notes="still a draft")
    policies.transition(session, policy, version, "REVIEW", actor_user_id=test_user.id)
    session.commit()
    with pytest.raises(ConflictError, match="cannot be edited"):
        policies.edit(session, policy, version, document=ACCESS_DOCUMENT)


def test_two_open_drafts_would_race(session, policy):
    with pytest.raises(ConflictError, match="still a draft"):
        policies.draft(session, policy, document=ACCESS_DOCUMENT)


def test_an_author_cannot_approve_their_own_policy(session, policy, test_user):
    """§5.2.10, unconditional — unlike the registry's change gate."""
    version = policies.get_version(session, policy.id, 1)
    policies.transition(session, policy, version, "REVIEW", actor_user_id=test_user.id)
    with pytest.raises(ForbiddenError, match="cannot approve it"):
        policies.transition(session, policy, version, "APPROVED", actor_user_id=test_user.id)


def test_the_refusal_names_the_fix(session, policy, test_user):
    version = policies.get_version(session, policy.id, 1)
    policies.transition(session, policy, version, "REVIEW", actor_user_id=test_user.id)
    with pytest.raises(ForbiddenError) as excinfo:
        policies.transition(session, policy, version, "APPROVED", actor_user_id=test_user.id)
    assert "another administrator" in str(excinfo.value).lower()


def test_a_second_admin_can_approve(session, policy, test_user, second_admin):
    version = activate(session, policy, author=test_user.id, approver=second_admin.id)
    assert version.state == "ACTIVE"
    assert version.approved_by_user_id == second_admin.id
    assert version.authored_by_user_id == test_user.id
    assert policy.active_version == 1


def test_review_can_send_a_version_back_to_draft(session, policy, test_user, second_admin):
    version = policies.get_version(session, policy.id, 1)
    policies.transition(session, policy, version, "REVIEW", actor_user_id=test_user.id)
    policies.transition(
        session, policy, version, "DRAFT", actor_user_id=second_admin.id, note="needs scoping"
    )
    session.commit()
    assert version.state == "DRAFT"
    assert version.decision_note == "needs scoping"
    # And it is editable again.
    policies.edit(session, policy, version, document=ACCESS_DOCUMENT)


def test_a_transition_the_lifecycle_forbids_names_both_ends(session, policy):
    version = policies.get_version(session, policy.id, 1)
    with pytest.raises(policies.LifecycleError) as excinfo:
        policies.transition(session, policy, version, "ACTIVE", actor_user_id=None)
    assert "DRAFT" in str(excinfo.value) and "ACTIVE" in str(excinfo.value)
    assert "REVIEW" in str(excinfo.value), "the message says what is possible"


# ── Activation, deployment and rollback ──────────────────────────────────────


def test_activating_deprecates_the_previous_active_version(
    session, policy, test_user, second_admin
):
    activate(session, policy, author=test_user.id, approver=second_admin.id)
    second = policies.draft(
        session, policy, document=ACCESS_DOCUMENT, authored_by_user_id=test_user.id
    )
    policies.transition(session, policy, second, "REVIEW", actor_user_id=test_user.id)
    policies.transition(session, policy, second, "APPROVED", actor_user_id=second_admin.id)
    policies.transition(session, policy, second, "PUBLISHED", actor_user_id=second_admin.id)
    policies.activate(session, policy, second)
    session.commit()

    first = policies.get_version(session, policy.id, 1)
    assert first.state == "DEPRECATED"
    assert second.state == "ACTIVE"
    assert policy.active_version == 2
    assert policy.previous_active_version == 1


def test_rollback_restores_the_previous_active_version(session, policy, test_user, second_admin):
    """§5.2.11: policy deploy failure — roll back to last active version."""
    activate(session, policy, author=test_user.id, approver=second_admin.id)
    second = policies.draft(
        session, policy, document=ACCESS_DOCUMENT, authored_by_user_id=test_user.id
    )
    policies.transition(session, policy, second, "REVIEW", actor_user_id=test_user.id)
    policies.transition(session, policy, second, "APPROVED", actor_user_id=second_admin.id)
    policies.transition(session, policy, second, "PUBLISHED", actor_user_id=second_admin.id)
    policies.activate(session, policy, second)
    session.commit()

    restored = policies.rollback(session, policy, reason="broke every refund")
    session.commit()
    assert restored == 1
    assert policy.active_version == 1
    assert policies.get_version(session, policy.id, 1).state == "ACTIVE"
    assert policies.get_version(session, policy.id, 2).state == "DEPRECATED"


def test_rollback_with_nothing_to_restore_is_refused(session, policy):
    with pytest.raises(ConflictError, match="no previous active version"):
        policies.rollback(session, policy)


def test_deprecating_the_active_version_stops_it_being_enforced(
    session, policy, test_user, second_admin
):
    version = activate(session, policy, author=test_user.id, approver=second_admin.id)
    policies.transition(session, policy, version, "DEPRECATED", actor_user_id=second_admin.id)
    session.commit()
    assert policy.active_version is None
    assert policies.active_rules(session, org_id=policy.org_id) == []


# ── Documents ────────────────────────────────────────────────────────────────


def test_a_policy_that_states_nothing_is_refused(session, test_org):
    with pytest.raises(InvalidRequestError, match="non-empty"):
        policies.create(
            session, org_id=test_org.id, key="empty-one", name="Empty", document={"rules": []}
        )


def test_a_rule_with_a_misspelled_attribute_is_refused_at_write_time(session, test_org):
    """The same guard standalone rules apply — a policy is not a way round it."""
    with pytest.raises(InvalidRequestError, match="Unknown resource attribute"):
        policies.create(
            session,
            org_id=test_org.id,
            key="typo-policy",
            name="Typo",
            document={
                "rules": [
                    {
                        "name": "x",
                        "effect": "deny",
                        "subject": {"role": "support"},
                        "resource": {"integraton_id": "payments"},
                    }
                ]
            },
        )


def test_a_duplicate_rule_name_within_a_policy_is_refused(session, test_org):
    rule = ACCESS_DOCUMENT["rules"][0]
    with pytest.raises(InvalidRequestError, match="appears twice"):
        policies.create(
            session,
            org_id=test_org.id,
            key="dupes",
            name="Dupes",
            document={"rules": [rule, rule]},
        )


def test_a_compliance_policy_is_prose_and_says_so(session, test_org):
    policy, version = policies.create(
        session,
        org_id=test_org.id,
        key="data-handling",
        name="Data handling",
        kind="compliance",
        document={"statement": "Customer data stays in the EU."},
    )
    session.commit()
    assert json.loads(version.document_json) == {"statement": "Customer data stays in the EU."}
    kinds = {entry["name"]: entry for entry in policies.describe()["kinds"]}
    assert kinds["compliance"]["evaluated"] is False


# ── Deployment: an active policy changes decisions ───────────────────────────


def _principal(org_id, role="support"):
    """An agent principal carrying a *business* role as an attribute.

    Agents are the realistic subject for these rules: an API key carries no
    platform role, so `role` on the subject is the tenant's own attribute.
    `test_a_platform_role_takes_precedence_over_an_attribute` pins what happens
    when a user principal has both.
    """
    import uuid

    return identity.Principal(
        urn=identity.urn(identity.KIND_AGENT, uuid.uuid4()),
        kind=identity.KIND_AGENT,
        org_id=org_id,
        attributes={"role": role},
    )


def _decide(session, org_id, role="support"):
    return pdp.authorize(
        session,
        principal=_principal(org_id, role),
        org_id=org_id,
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        permission="tools:execute",
    )


def test_a_platform_role_takes_precedence_over_an_attribute(
    session, policy, test_org, test_user, second_admin
):
    """A tenant cannot widen its own access by naming an attribute `role`.

    The platform role is authoritative and is written last, so an agent
    claiming `role: owner` is still evaluated as whatever the platform says it
    is — which for a user principal is their membership role.
    """
    import uuid as uuid_module

    activate(session, policy, author=test_user.id, approver=second_admin.id)
    liar = identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid_module.uuid4()),
        kind=identity.KIND_USER,
        org_id=test_org.id,
        role="member",
        attributes={"role": "support"},
    )
    decision = pdp.authorize(
        session,
        principal=liar,
        org_id=test_org.id,
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        permission="tools:execute",
    )
    # The deny rule matches `role: support`, and this principal is a `member`.
    assert decision.allowed is True


def test_a_draft_policy_changes_no_decision(session, policy, test_org):
    """This is what "deployed independently" buys: writing is not deploying."""
    decision = _decide(session, test_org.id)
    assert decision.allowed is True
    assert policies.active_rules(session, org_id=test_org.id) == []


def test_an_active_policy_contributes_its_rules(session, policy, test_org, test_user, second_admin):
    activate(session, policy, author=test_user.id, approver=second_admin.id)
    decision = _decide(session, test_org.id)
    assert decision.allowed is False
    assert decision.denied_by == "abac"
    assert "no-refunds-outside-finance" in decision.reason
    abac = next(layer for layer in decision.layers if layer.layer == "abac")
    match = abac.detail["matches"][0]
    assert match["source"] == "policy"
    assert match["policy_key"] == "refund-controls"
    assert match["policy_version"] == 1


def test_a_policy_deny_is_not_overruled_by_a_standalone_allow(
    session, policy, test_org, test_user, second_admin
):
    activate(session, policy, author=test_user.id, approver=second_admin.id)
    rules.create(
        session,
        org_id=test_org.id,
        name="support-may-refund",
        effect="allow",
        subject={"role": "support"},
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        priority=1,
    )
    session.commit()
    assert _decide(session, test_org.id).allowed is False


def test_a_policy_from_another_tenant_is_not_read(
    session, policy, other_org, test_user, second_admin
):
    activate(session, policy, author=test_user.id, approver=second_admin.id)
    assert policies.active_rules(session, org_id=other_org.id) == []
    assert _decide(session, other_org.id).allowed is True
