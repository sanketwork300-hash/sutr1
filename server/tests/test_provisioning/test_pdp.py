"""The four authorization layers, in order (LLD §4.3)."""

import uuid

import pytest

from sutr.provisioning import identity, pdp, rules

from .conftest import principal_for


def _user_principal(org_id, role="developer"):
    return identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid.uuid4()),
        kind=identity.KIND_USER,
        org_id=org_id,
        display="somebody@example.com",
        role=role,
    )


def _decide(session, principal, org_id, **overrides):
    kwargs = {
        "resource": {"integration_id": "payments", "tool_name": "refund_payment"},
        "action": "invoke",
        "permission": "tools:execute",
    }
    kwargs.update(overrides)
    return pdp.authorize(session, principal=principal, org_id=org_id, **kwargs)


# ── Ordering ─────────────────────────────────────────────────────────────────


def test_the_layers_run_in_the_lld_order(session, test_org):
    decision = _decide(session, _user_principal(test_org.id), test_org.id)
    assert [layer.layer for layer in decision.layers] == ["tenant", "rbac", "abac"]
    assert decision.as_dict()["layer_order"] == ["tenant", "rbac", "abac", "policy"]


def test_the_first_denial_stops_the_chain(session, test_org, other_org):
    """A cross-tenant caller must not cause this tenant's rules to be loaded."""
    principal = _user_principal(other_org.id)
    decision = _decide(session, principal, test_org.id)
    assert decision.allowed is False
    assert decision.denied_by == "tenant"
    assert [layer.layer for layer in decision.layers] == ["tenant"]
    # The layers that did not run are absent, not recorded as agreeing.
    assert "abac" not in {layer.layer for layer in decision.layers}


def test_layer_four_is_delegated_and_says_so(session, test_org):
    decision = _decide(session, _user_principal(test_org.id), test_org.id)
    assert "policy" not in {layer.layer for layer in decision.layers}
    assert "enforcement point immediately after" in decision.as_dict()["note"]
    described = pdp.describe()
    policy_layer = next(layer for layer in described["layers"] if layer["name"] == "policy")
    assert policy_layer["decided_here"] is False
    assert policy_layer["order"] == 4


# ── Layer 1: tenant ──────────────────────────────────────────────────────────


def test_acting_outside_your_tenant_is_refused_with_both_orgs_named(session, test_org, other_org):
    decision = _decide(session, _user_principal(other_org.id), test_org.id)
    assert "outside their own organization" in decision.reason
    detail = decision.layers[0].detail
    assert detail["principal_org"] == str(other_org.id)
    assert detail["resource_org"] == str(test_org.id)


# ── Layer 2: RBAC ────────────────────────────────────────────────────────────


def test_a_role_without_the_permission_is_refused(session, test_org):
    decision = _decide(session, _user_principal(test_org.id, role="viewer"), test_org.id)
    assert decision.allowed is False
    assert decision.denied_by == "rbac"
    assert "'viewer' does not hold 'tools:execute'" in decision.reason


def test_a_principal_with_no_role_abstains_rather_than_being_invented_one(session, test_org):
    """API keys carry no role. The layer says so; it does not guess."""
    principal = identity.Principal(
        urn=identity.urn(identity.KIND_API_KEY, uuid.uuid4()),
        kind=identity.KIND_API_KEY,
        org_id=test_org.id,
    )
    decision = _decide(session, principal, test_org.id)
    rbac = next(layer for layer in decision.layers if layer.layer == "rbac")
    assert rbac.effect == pdp.ABSTAIN
    assert "carries no role" in rbac.reason
    assert "access rule" in rbac.reason, "and it names the way to close the gap"
    assert decision.allowed is True


def test_an_unknown_permission_is_a_denial_not_a_pass(session, test_org):
    decision = _decide(session, _user_principal(test_org.id), test_org.id, permission="nope:nope")
    assert decision.allowed is False
    assert decision.denied_by == "rbac"


def test_no_permission_named_means_the_layer_abstains(session, test_org):
    decision = _decide(session, _user_principal(test_org.id), test_org.id, permission=None)
    rbac = next(layer for layer in decision.layers if layer.layer == "rbac")
    assert rbac.effect == pdp.ABSTAIN
    assert decision.allowed is True


# ── Layer 3: ABAC ────────────────────────────────────────────────────────────


def test_the_llds_worked_example(session, test_org, agent, refund_rule):
    """Finance role ∧ Region=India ⇒ Allow Refund Tool."""
    decision = _decide(session, principal_for(agent), test_org.id)
    assert decision.allowed is True
    abac = next(layer for layer in decision.layers if layer.layer == "abac")
    assert abac.effect == pdp.ALLOW
    assert "finance-india-refunds" in abac.reason
    assert abac.detail["matches"][0]["matched"] == {
        "role": "finance",
        "region": "india",
        "integration_id": "payments",
        "tool_name": "refund_payment",
    }


def test_the_same_rule_does_not_match_a_different_region(session, test_org, agent, refund_rule):
    from sutr.provisioning import identity as identity_module

    identity_module.update(session, agent, {"attributes": {"role": "finance", "region": "France"}})
    session.commit()
    decision = _decide(session, principal_for(agent), test_org.id)
    abac = next(layer for layer in decision.layers if layer.layer == "abac")
    # No rule matched, so no opinion — not a denial.
    assert abac.effect == pdp.ABSTAIN
    assert "No access rule applies" in abac.reason
    assert decision.allowed is True


def test_a_tenant_with_no_rules_keeps_the_behaviour_it_had(session, test_org, agent):
    """A layer that starts denying the day it ships is a layer nobody turns on."""
    decision = _decide(session, principal_for(agent), test_org.id)
    abac = next(layer for layer in decision.layers if layer.layer == "abac")
    assert abac.effect == pdp.ABSTAIN
    assert "no access rules" in abac.reason
    assert decision.allowed is True


def test_deny_beats_allow_whatever_the_priority(session, test_org, agent, refund_rule):
    rules.create(
        session,
        org_id=test_org.id,
        name="no-refunds-during-freeze",
        effect="deny",
        subject={"role": "finance"},
        resource={"integration_id": "payments"},
        # Deliberately the *lower*-priority rule, to prove priority does not
        # decide the outcome.
        priority=9000,
    )
    session.commit()
    decision = _decide(session, principal_for(agent), test_org.id)
    assert decision.allowed is False
    assert decision.denied_by == "abac"
    assert "no-refunds-during-freeze" in decision.reason


def test_a_disabled_rule_does_not_fire(session, test_org, agent, refund_rule):
    deny = rules.create(
        session,
        org_id=test_org.id,
        name="temporarily-off",
        effect="deny",
        subject={"role": "finance"},
        resource={"integration_id": "payments"},
    )
    rules.set_enabled(session, deny, False)
    session.commit()
    assert _decide(session, principal_for(agent), test_org.id).allowed is True


def test_rules_are_scoped_to_their_tenant(session, test_org, other_org, agent):
    rules.create(
        session,
        org_id=other_org.id,
        name="their-deny",
        effect="deny",
        subject={"role": "finance"},
        resource={"integration_id": "payments"},
    )
    session.commit()
    assert _decide(session, principal_for(agent), test_org.id).allowed is True


def test_attribute_comparison_is_case_insensitive(session, test_org, agent):
    """A rule saying "India" and an identity saying "india" must match.

    A silently-never-matching rule is the worst failure an authorization layer
    can have, so both sides are normalised on the way in.
    """
    rules.create(
        session,
        org_id=test_org.id,
        name="mixed-case",
        effect="deny",
        subject={"Region": "INDIA"},
        resource={"integration_id": "PAYMENTS"},
    )
    session.commit()
    decision = _decide(session, principal_for(agent), test_org.id)
    assert decision.allowed is False


def test_a_rule_matching_only_the_subject_still_applies(session, test_org, agent):
    """An absent matcher matches anything; it is not an implicit mismatch."""
    rules.create(
        session,
        org_id=test_org.id,
        name="finance-blocked-everywhere",
        effect="deny",
        subject={"role": "finance"},
    )
    session.commit()
    assert _decide(session, principal_for(agent), test_org.id).allowed is False


def test_an_action_a_rule_was_not_written_for_is_unaffected(session, test_org, agent):
    rules.create(
        session,
        org_id=test_org.id,
        name="no-installing",
        effect="deny",
        subject={"role": "finance"},
        action="install",
    )
    session.commit()
    assert _decide(session, principal_for(agent), test_org.id, action="invoke").allowed is True
    assert _decide(session, principal_for(agent), test_org.id, action="install").allowed is False


# ── Authoring guards ─────────────────────────────────────────────────────────


def test_a_rule_matching_everything_is_refused(session, test_org):
    from sutr.common.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError, match="matches everything"):
        rules.create(session, org_id=test_org.id, name="everything", effect="deny")


def test_a_misspelled_resource_attribute_is_refused_at_write_time(session, test_org):
    """A matcher that never matches is worse than no rule."""
    from sutr.common.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError, match="Unknown resource attribute"):
        rules.create(
            session,
            org_id=test_org.id,
            name="typo",
            effect="deny",
            resource={"integraton_id": "payments"},
        )


def test_a_decision_is_always_explainable(session, test_org, agent, refund_rule):
    payload = _decide(session, principal_for(agent), test_org.id).as_dict()
    assert payload["layers"]
    for layer in payload["layers"]:
        assert layer["effect"] in (pdp.ALLOW, pdp.DENY, pdp.ABSTAIN)
        assert layer["reason"], f"{layer['layer']} gave no reason"


def test_the_layer_source_is_stated_rather_than_asserted():
    """The LLD's figure labels are not extractable; the derivation is recorded."""
    assert "not extractable" in pdp.describe()["source"]


def test_an_empty_matcher_value_is_dropped_rather_than_matching_nothing(session, test_org):
    """Found live: `{"integration_id": ""}` was accepted and never matched.

    A rule that silently never matches is the worst failure this layer can
    have, so an empty value is dropped. Here that leaves only the subject
    matcher, which is a rule somebody can read.
    """
    from sutr.common.errors import InvalidRequestError

    rule = rules.create(
        session,
        org_id=test_org.id,
        name="finance-blocked",
        effect="deny",
        subject={"role": "finance"},
        resource={"integration_id": ""},
    )
    session.commit()
    assert rules.serialize(rule)["resource"] == {}

    # And a rule left with nothing at all is refused rather than stored inert.
    with pytest.raises(InvalidRequestError, match="matches everything"):
        rules.create(
            session,
            org_id=test_org.id,
            name="nothing-at-all",
            effect="deny",
            subject={"role": "  "},
            resource={"integration_id": ""},
        )
