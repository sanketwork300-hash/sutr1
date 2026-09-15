"""Scoped access passes: the LLD's five adjectives, each as a property."""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlmodel import select

from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.config import settings
from sutr.models.access_pass import AccessPass
from sutr.provisioning import identity, passes, pdp

from .conftest import principal_for


def _decision(session, org_id, principal, tool="refund_payment"):
    return pdp.authorize(
        session,
        principal=principal,
        org_id=org_id,
        resource={"integration_id": "payments", "tool_name": tool},
        action="invoke",
        permission="tools:execute",
    )


def _issue(session, test_org, agent, **overrides):
    principal = principal_for(agent)
    kwargs = {
        "org_id": test_org.id,
        "principal": principal.urn,
        "decision": _decision(session, test_org.id, principal),
        "tools": ["refund_payment"],
        "resource": "payments",
        "purpose": "issue one refund",
        "agent_id": agent.id,
    }
    kwargs.update(overrides)
    return passes.issue(session, **kwargs)


# ── Signed, and in the format the generated runtimes already validate ────────


def test_a_pass_is_a_jwt_with_the_claims_the_generated_runtime_checks(session, test_org, agent):
    """Phase 5's generated servers have been validating this shape since they
    shipped; the issuer had to match them, not the other way round."""
    issued = _issue(session, test_org, agent)
    session.commit()
    claims = jwt.decode(
        issued.token,
        passes.signing_key(),
        algorithms=["HS256"],
        audience="payments",
        issuer="sutr",
    )
    assert set(claims) >= {"iss", "aud", "sub", "exp", "jti", "nonce", "tenant_id", "tools"}
    assert claims["tenant_id"] == str(test_org.id)
    assert claims["tools"] == ["refund_payment"]
    assert claims["sub"] == identity.urn(identity.KIND_AGENT, agent.id)


def test_the_signing_key_is_separated_from_the_session_token_key(monkeypatch):
    """One configured secret, two cryptographically distinct keys."""
    monkeypatch.setattr(settings, "access_pass_secret", "")
    derived = passes.signing_key()
    assert derived != settings.jwt_secret_key.encode()
    assert len(derived) == 32
    monkeypatch.setattr(settings, "access_pass_secret", "an-explicit-secret")
    assert passes.signing_key() == b"an-explicit-secret"


def test_a_pass_minted_for_one_resource_is_rejected_by_another(session, test_org, agent):
    """`aud` is the resource, so single-purpose is enforced by the verifier."""
    issued = _issue(session, test_org, agent, resource="payments")
    session.commit()
    passes.verify(session, issued.token, resource="payments")
    with pytest.raises(passes.PassError, match="rejected"):
        passes.verify(session, issued.token, resource="shipping")


# ── The token is not stored ──────────────────────────────────────────────────


def test_the_token_is_never_written_to_the_database(session, test_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    record = session.exec(select(AccessPass)).one()
    stored = json.dumps(record.model_dump(mode="json"))
    assert issued.token not in stored
    assert issued.as_dict()["stored"] is False
    assert "not stored" in issued.as_dict()["storage_note"]


def test_the_decision_that_granted_it_is_stored(session, test_org, agent, refund_rule):
    issued = _issue(session, test_org, agent)
    session.commit()
    decision = json.loads(issued.record.decision_json)
    assert decision["allowed"] is True
    assert [layer["layer"] for layer in decision["layers"]] == ["tenant", "rbac", "abac"]


# ── Least privilege ──────────────────────────────────────────────────────────


def test_asking_for_more_than_you_may_have_narrows_the_pass(session, test_org, agent):
    issued = _issue(
        session,
        test_org,
        agent,
        tools=["refund_payment", "delete_everything"],
        entitled_tools=["refund_payment"],
    )
    session.commit()
    assert issued.granted_tools == ["refund_payment"]
    assert issued.narrowed_from == ["refund_payment", "delete_everything"]
    assert "Least privilege" in issued.as_dict()["narrowed_reason"]


def test_a_pass_covering_nothing_is_refused_rather_than_issued_empty(session, test_org, agent):
    with pytest.raises(ConflictError, match="nothing to issue"):
        _issue(session, test_org, agent, tools=["nope"], entitled_tools=["refund_payment"])


def test_a_pass_must_name_its_tools(session, test_org, agent):
    with pytest.raises(InvalidRequestError, match="opposite of least privilege"):
        _issue(session, test_org, agent, tools=[])


def test_a_pass_may_not_cover_unbounded_tools(session, test_org, agent):
    with pytest.raises(InvalidRequestError, match="at most"):
        _issue(session, test_org, agent, tools=[f"tool_{i}" for i in range(passes.MAX_TOOLS + 1)])


# ── Issued after the decision, never instead of it ───────────────────────────


def test_a_refused_decision_cannot_produce_a_pass(session, test_org, other_org, agent):
    """ "Issued by Provisioning after the policy decision" — enforced, not implied."""
    outsider = identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid.uuid4()),
        kind=identity.KIND_USER,
        org_id=other_org.id,
        role="owner",
    )
    refused = pdp.authorize(
        session,
        principal=outsider,
        org_id=test_org.id,
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        permission="tools:execute",
    )
    assert refused.allowed is False
    with pytest.raises(ConflictError, match="refused decision"):
        passes.issue(
            session,
            org_id=test_org.id,
            principal=outsider.urn,
            decision=refused,
            tools=["refund_payment"],
        )


# ── Short-lived ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ttl", [passes.MIN_TTL_SECONDS - 1, passes.MAX_TTL_SECONDS + 1])
def test_a_lifetime_outside_the_window_is_refused(session, test_org, agent, ttl):
    with pytest.raises(InvalidRequestError, match="lives between"):
        _issue(session, test_org, agent, ttl_seconds=ttl)


def test_an_expired_pass_is_rejected(session, test_org, agent):
    issued = _issue(session, test_org, agent, ttl_seconds=passes.MIN_TTL_SECONDS)
    session.commit()
    # Rewind the clock by re-signing the same claims with a past expiry rather
    # than sleeping: the property under test is the verifier's, not time's.
    claims = jwt.decode(
        issued.token,
        passes.signing_key(),
        algorithms=["HS256"],
        audience="payments",
        issuer="sutr",
    )
    claims["exp"] = int(time.time()) - 1
    stale = jwt.encode(claims, passes.signing_key(), algorithm="HS256")
    with pytest.raises(passes.PassError, match="expired"):
        passes.verify(session, stale, resource="payments")


def test_status_reports_expiry_without_needing_a_verification(session, test_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    assert passes.status_of(issued.record) == "active"
    issued.record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert passes.status_of(issued.record) == "expired"


# ── Revocable ────────────────────────────────────────────────────────────────


def test_a_revoked_pass_is_rejected_with_the_reason(session, test_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    passes.revoke(session, issued.record, reason="the agent was compromised")
    session.commit()
    with pytest.raises(passes.PassError, match="the agent was compromised"):
        passes.verify(session, issued.token, resource="payments")


def test_revoking_twice_is_refused(session, test_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    passes.revoke(session, issued.record)
    with pytest.raises(ConflictError, match="already revoked"):
        passes.revoke(session, issued.record)


def test_every_live_pass_for_a_principal_can_be_revoked_at_once(session, test_org, agent):
    """An incident revokes what a credential already minted, not just the key."""
    first = _issue(session, test_org, agent)
    second = _issue(session, test_org, agent, purpose="another")
    session.commit()
    revoked = passes.revoke_for_principal(
        session,
        org_id=test_org.id,
        principal=principal_for(agent).urn,
        reason="incident 42",
    )
    session.commit()
    assert revoked == 2
    for issued in (first, second):
        with pytest.raises(passes.PassError, match="incident 42"):
            passes.verify(session, issued.token, resource="payments")


def test_revocation_does_not_reach_another_tenants_passes(session, test_org, other_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    assert (
        passes.revoke_for_principal(
            session, org_id=other_org.id, principal=principal_for(agent).urn, reason="x"
        )
        == 0
    )
    session.commit()
    assert passes.verify(session, issued.token, resource="payments") is not None


# ── Verification ─────────────────────────────────────────────────────────────


def test_a_pass_that_does_not_cover_the_tool_is_rejected(session, test_org, agent):
    issued = _issue(session, test_org, agent, tools=["refund_payment"])
    session.commit()
    passes.verify(session, issued.token, resource="payments", tool_name="refund_payment")
    with pytest.raises(passes.PassError, match="does not cover"):
        passes.verify(session, issued.token, resource="payments", tool_name="delete_everything")


def test_a_pass_this_platform_did_not_issue_is_rejected(session, test_org):
    """A valid signature over a record that does not exist is not a pass."""
    claims = {
        "iss": "sutr",
        "aud": "payments",
        "sub": "sutr:agent:whoever",
        "jti": str(uuid.uuid4()),
        "nonce": "x",
        "exp": int(time.time()) + 60,
        "tenant_id": str(test_org.id),
        "tools": ["refund_payment"],
    }
    forged = jwt.encode(claims, passes.signing_key(), algorithm="HS256")
    with pytest.raises(passes.PassError, match="not on record"):
        passes.verify(session, forged, resource="payments")


def test_a_pass_signed_with_the_wrong_key_is_rejected(session, test_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    claims = jwt.decode(
        issued.token,
        passes.signing_key(),
        algorithms=["HS256"],
        audience="payments",
        issuer="sutr",
    )
    forged = jwt.encode(claims, b"not-the-key", algorithm="HS256")
    with pytest.raises(passes.PassError, match="rejected"):
        passes.verify(session, forged, resource="payments")


def test_use_is_recorded_without_claiming_to_be_the_enforcement(session, test_org, agent):
    issued = _issue(session, test_org, agent)
    session.commit()
    passes.verify(session, issued.token, resource="payments")
    passes.verify(session, issued.token, resource="payments")
    session.commit()
    session.refresh(issued.record)
    assert issued.record.use_count == 2
    assert issued.record.first_seen_at is not None
    # Single use is enforced in the generated runtime, in process. This is a
    # record, and `describe()` says which is which.
    described = passes.describe()
    assert described["revocation"]["enforced_by_generated_runtimes"] is False
    assert "cannot see a revocation" in described["revocation"]["detail"]


def test_the_description_does_not_overclaim_storage():
    assert passes.describe()["token_storage"].startswith("none")
