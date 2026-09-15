"""The /v1/provisioning surface, and the enforcement point it feeds."""

import jwt
from sqlmodel import select

from sutr.models.access_pass import AccessPass
from sutr.models.audit_event import AuditEvent
from sutr.models.org_membership import OrgMembership
from sutr.provisioning import passes


async def _register(client, **overrides):
    body = {
        "name": "finance-agent",
        "description": "Issues refunds.",
        "attributes": {"role": "finance", "region": "India"},
    }
    body.update(overrides)
    response = await client.post("/v1/provisioning/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _rule(client, **overrides):
    body = {
        "name": "finance-india-refunds",
        "effect": "allow",
        "subject": {"role": "finance", "region": "India"},
        "resource": {"integration_id": "payments", "tool_name": "refund_payment"},
    }
    body.update(overrides)
    response = await client.post("/v1/provisioning/rules", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ── Identity ─────────────────────────────────────────────────────────────────


async def test_registering_an_agent_gives_it_a_principal_urn(client, session):
    data = await _register(client)
    assert data["principal"] == f"sutr:agent:{data['id']}"
    assert data["attributes"] == {"role": "finance", "region": "india"}
    assert data["attributes_are_tenant_declared"] is True
    session.expire_all()
    assert session.exec(
        select(AuditEvent).where(AuditEvent.action == "provisioning.agent_registered")
    ).first()


async def test_whoami_names_an_unbound_key_as_a_key_not_an_agent(client):
    """An actor with no identity is said plainly rather than dressed as one."""
    response = await client.get("/v1/provisioning/whoami")
    data = response.json()["data"]
    # The test client authenticates as a user, so that is what it reports.
    assert data["kind"] == "user"
    assert data["principal"].startswith("sutr:user:")
    assert data["role"] == "owner"


async def test_a_duplicate_agent_name_is_a_conflict(client):
    await _register(client)
    response = await client.post("/v1/provisioning/agents", json={"name": "finance-agent"})
    assert response.status_code == 409


async def test_an_agent_name_that_cannot_be_a_urn_is_refused(client):
    response = await client.post("/v1/provisioning/agents", json={"name": "Finance Agent!"})
    assert response.status_code in (400, 422)


async def test_revoking_an_agent_revokes_its_live_passes(client, session):
    agent = await _register(client)
    await _rule(client)
    issued = await client.post(
        "/v1/provisioning/passes",
        json={
            "tools": ["refund_payment"],
            "integration_id": "payments",
            "as_agent_id": agent["id"],
        },
    )
    assert issued.status_code == 201, issued.text

    revoked = await client.post(
        f"/v1/provisioning/agents/{agent['id']}/revoke", json={"reason": "compromised"}
    )
    body = revoked.json()["data"]
    assert body["active"] is False
    assert body["passes_revoked"] == 1

    session.expire_all()
    record = session.exec(select(AccessPass)).one()
    assert record.revoked_at is not None
    assert record.revoked_reason == "compromised"


async def test_a_revoked_agent_cannot_be_issued_a_new_pass(client):
    agent = await _register(client)
    await client.post(f"/v1/provisioning/agents/{agent['id']}/revoke", json={})
    response = await client.post(
        "/v1/provisioning/passes",
        json={
            "tools": ["refund_payment"],
            "integration_id": "payments",
            "as_agent_id": agent["id"],
        },
    )
    assert response.status_code == 400
    assert "revoked" in response.json()["error"]["message"]


# ── Rules and decisions ──────────────────────────────────────────────────────


async def test_a_decision_explains_every_layer(client):
    agent = await _register(client)
    await _rule(client)
    response = await client.post(
        "/v1/provisioning/decisions",
        json={
            "integration_id": "payments",
            "tool_name": "refund_payment",
            "as_agent_id": agent["id"],
        },
    )
    data = response.json()["data"]
    assert data["allowed"] is True
    assert [layer["layer"] for layer in data["layers"]] == ["tenant", "rbac", "abac"]
    assert data["evaluated_as"]["principal"] == agent["principal"]
    assert all(layer["reason"] for layer in data["layers"])


async def test_a_decision_changes_nothing(client, session):
    agent = await _register(client)
    await client.post(
        "/v1/provisioning/decisions",
        json={
            "integration_id": "payments",
            "tool_name": "refund_payment",
            "as_agent_id": agent["id"],
        },
    )
    session.expire_all()
    assert session.exec(select(AccessPass)).all() == []


async def test_a_deny_rule_refuses_the_decision_with_its_name(client):
    agent = await _register(client)
    await _rule(client, name="frozen", effect="deny", resource={"integration_id": "payments"})
    response = await client.post(
        "/v1/provisioning/decisions",
        json={
            "integration_id": "payments",
            "tool_name": "refund_payment",
            "as_agent_id": agent["id"],
        },
    )
    data = response.json()["data"]
    assert data["allowed"] is False
    assert data["denied_by"] == "abac"
    assert "frozen" in data["reason"]


async def test_the_rule_list_states_how_rules_are_evaluated(client):
    await _rule(client)
    response = await client.get("/v1/provisioning/rules")
    data = response.json()["data"]
    assert len(data["rules"]) == 1
    assert "Deny wins" in data["evaluation"]
    assert "integration_id" in data["resource_attributes"]


# ── Passes ───────────────────────────────────────────────────────────────────


async def test_a_pass_is_issued_after_the_decision_and_returned_once(client, session):
    agent = await _register(client)
    await _rule(client)
    response = await client.post(
        "/v1/provisioning/passes",
        json={
            "tools": ["refund_payment"],
            "integration_id": "payments",
            "purpose": "issue one refund",
            "ttl_seconds": 120,
            "as_agent_id": agent["id"],
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 120
    assert data["tools"] == ["refund_payment"]
    assert data["stored"] is False
    assert data["decision"]["allowed"] is True

    claims = jwt.decode(
        data["access_pass"],
        passes.signing_key(),
        algorithms=["HS256"],
        audience="payments",
        issuer="sutr",
    )
    assert claims["sub"] == agent["principal"]

    # And re-reading the pass never returns the token again.
    reread = await client.get(f"/v1/provisioning/passes/{data['id']}")
    assert "access_pass" not in reread.json()["data"]


async def test_a_pass_is_narrowed_to_what_was_allowed(client):
    agent = await _register(client)
    await _rule(client)
    await _rule(
        client,
        name="no-deletes",
        effect="deny",
        resource={"integration_id": "payments", "tool_name": "delete_everything"},
        subject={"role": "finance"},
    )
    response = await client.post(
        "/v1/provisioning/passes",
        json={
            "tools": ["refund_payment", "delete_everything"],
            "integration_id": "payments",
            "as_agent_id": agent["id"],
        },
    )
    data = response.json()["data"]
    assert data["tools"] == ["refund_payment"]
    assert data["narrowed_from"] == ["refund_payment", "delete_everything"]


async def test_a_pass_for_nothing_permitted_is_refused_with_the_reason(client):
    agent = await _register(client)
    await _rule(client, name="all-denied", effect="deny", subject={"role": "finance"})
    response = await client.post(
        "/v1/provisioning/passes",
        json={
            "tools": ["refund_payment"],
            "integration_id": "payments",
            "as_agent_id": agent["id"],
        },
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "authorization_denied"
    assert "all-denied" in body["message"]


async def test_revoking_a_pass_says_what_revocation_does_not_reach(client):
    agent = await _register(client)
    await _rule(client)
    issued = await client.post(
        "/v1/provisioning/passes",
        json={
            "tools": ["refund_payment"],
            "integration_id": "payments",
            "as_agent_id": agent["id"],
        },
    )
    pass_id = issued.json()["data"]["id"]
    response = await client.post(
        f"/v1/provisioning/passes/{pass_id}/revoke", json={"reason": "no longer needed"}
    )
    data = response.json()["data"]
    assert data["status"] == "revoked"
    assert "validates offline and cannot see it" in data["note"]


async def test_capabilities_reports_the_layers_the_passes_and_the_secret_store(client):
    response = await client.get("/v1/provisioning/capabilities")
    data = response.json()["data"]
    assert [layer["name"] for layer in data["authorization"]["layers"]] == [
        "tenant",
        "rbac",
        "abac",
        "policy",
    ]
    assert data["access_passes"]["revocation"]["enforced_by_generated_runtimes"] is False
    assert data["secrets"]["at_rest"] == "plaintext in the database"
    assert data["secrets"]["dynamic_credentials"]["available"] is False
    assert "NOT IMPLEMENTED" in data["secrets"]["dynamic_credentials"]["unavailable_reason"]


async def test_writing_rules_needs_more_than_read_access(client, session, test_user):
    membership = session.exec(
        select(OrgMembership).where(OrgMembership.user_id == test_user.id)
    ).one()
    membership.role = "developer"
    session.add(membership)
    session.commit()
    response = await client.post(
        "/v1/provisioning/rules", json={"name": "nope", "effect": "deny", "subject": {"role": "x"}}
    )
    assert response.status_code == 403
