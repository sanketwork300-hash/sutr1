"""The enforcement point: authorization layers refusing a real tool call.

A policy decision point nobody consults is a library. These tests go through
`evaluate_gate`, which is what the REST and MCP surfaces both call.
"""

import pytest
from sqlmodel import select

from sutr.models.log import LogEntry
from sutr.provisioning import identity, rules
from sutr.services.tool_pipeline import CallContext, evaluate_gate


def _context(session, test_org, principal=None):
    return CallContext(org_id=test_org.id, source="api", principal=principal)


def _gate(session, ctx, integration_id="payments", tool_name="refund_payment"):
    return evaluate_gate(session, ctx, integration_id, tool_name, {})


def test_a_deny_rule_refuses_the_call_at_the_gate(session, test_org, agent):
    from .conftest import principal_for

    rules.create(
        session,
        org_id=test_org.id,
        name="frozen",
        effect="deny",
        subject={"role": "finance"},
        resource={"integration_id": "payments"},
    )
    session.commit()

    result = _gate(session, _context(session, test_org, principal_for(agent)))
    assert result.status == "denied"
    assert result.authorization["denied_by"] == "abac"
    assert "frozen" in result.authorization["reason"]


def test_the_refusal_is_logged_with_the_reason_not_just_a_verdict(session, test_org, agent):
    """LLD §4.2's hot path asks for "403 + policy reason"."""
    from .conftest import principal_for

    rules.create(
        session,
        org_id=test_org.id,
        name="frozen",
        effect="deny",
        subject={"role": "finance"},
        resource={"integration_id": "payments"},
    )
    session.commit()
    _gate(session, _context(session, test_org, principal_for(agent)))

    session.expire_all()
    entry = session.exec(select(LogEntry).where(LogEntry.outcome == "denied")).one()
    assert entry.error.startswith("abac:")
    assert "frozen" in entry.error


def test_a_cross_tenant_principal_is_refused_at_the_gate(session, test_org, other_org):
    import uuid as uuid_module

    principal = identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid_module.uuid4()),
        kind=identity.KIND_USER,
        org_id=other_org.id,
        role="owner",
    )
    result = _gate(session, _context(session, test_org, principal))
    assert result.status == "denied"
    assert result.authorization["denied_by"] == "tenant"


def test_a_viewer_is_refused_by_the_rbac_layer(session, test_org):
    import uuid as uuid_module

    principal = identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid_module.uuid4()),
        kind=identity.KIND_USER,
        org_id=test_org.id,
        role="viewer",
    )
    result = _gate(session, _context(session, test_org, principal))
    assert result.status == "denied"
    assert result.authorization["denied_by"] == "rbac"


def test_a_call_with_no_principal_behaves_exactly_as_before(session, test_org):
    """Every existing caller keeps working: no principal, no new layer.

    This is what makes the layer safe to add to a hot path that hundreds of
    tests already cover — and those tests passing unchanged is the evidence.
    """
    result = _gate(session, _context(session, test_org, principal=None))
    assert result.status != "denied"
    assert result.authorization is None


def test_an_allowed_call_reaches_the_per_tool_policy(session, test_org, agent, refund_rule):
    """Layer 4 is the existing policy, and it still runs after layers 1–3."""
    from .conftest import principal_for

    result = _gate(session, _context(session, test_org, principal_for(agent)))
    # Not denied by authorization; the outcome now belongs to the tool policy,
    # which defaults to requiring approval.
    assert result.status in ("approval_pending", "ready")
    assert result.authorization is None


def test_the_principal_is_what_the_log_attributes_the_call_to(session, test_org, agent):
    from .conftest import principal_for

    ctx = _context(session, test_org, principal_for(agent))
    assert ctx.requested_by_agent == f"sutr:agent:{agent.id}"


def test_an_api_key_without_an_identity_is_still_attributable(session, test_org, api_key):
    """ "Unidentified caller" is legible rather than absent."""
    principal = identity.Principal(
        urn=identity.urn(identity.KIND_API_KEY, api_key.id),
        kind=identity.KIND_API_KEY,
        org_id=test_org.id,
    )
    ctx = _context(session, test_org, principal)
    assert ctx.requested_by_agent == f"sutr:api-key:{api_key.id}"


def test_a_bound_key_resolves_to_its_agent(session, test_org, agent, api_key):
    from sutr.dependencies import AgentAuth

    auth = AgentAuth(org=test_org, user=None, api_key=api_key)
    principal = identity.for_agent_auth(session, auth)
    assert principal.kind == "agent"
    assert principal.attributes["role"] == "finance"


def test_an_unbound_key_resolves_to_a_key_principal(session, test_org, api_key, agent):
    from sutr.dependencies import AgentAuth
    from sutr.models.api_key import ApiKey

    other = ApiKey(
        org_id=test_org.id,
        created_by_user_id=agent.created_by_user_id,
        name="unbound",
        key_prefix="sk_test_9999",
        key_hash="9" * 64,
    )
    session.add(other)
    session.commit()
    principal = identity.for_agent_auth(session, AgentAuth(org=test_org, user=None, api_key=other))
    assert principal.kind == "api-key"
    assert principal.attributes == {}


def test_a_revoked_agents_key_stops_resolving_to_it(session, test_org, agent, api_key):
    from sutr.dependencies import AgentAuth

    identity.revoke(session, agent, reason="compromised")
    session.commit()
    principal = identity.for_agent_auth(
        session, AgentAuth(org=test_org, user=None, api_key=api_key)
    )
    assert principal.kind == "api-key", "a revoked identity no longer speaks for the key"


def test_one_key_cannot_be_bound_to_two_identities(session, test_org, agent, api_key):
    from sutr.common.errors import ConflictError

    with pytest.raises(ConflictError, match="already bound"):
        identity.register(session, org_id=test_org.id, name="second-agent", api_key_id=api_key.id)
