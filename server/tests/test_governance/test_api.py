"""The /v1/governance surface (LLD §5.2.13)."""

import uuid

from sqlmodel import select

from sutr.models.audit_event import AuditEvent
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User

DOCUMENT = {
    "rules": [
        {
            "name": "no-refunds-outside-finance",
            "effect": "deny",
            "subject": {"role": "support"},
            "resource": {"integration_id": "payments", "tool_name": "refund_payment"},
        }
    ]
}


async def _create_policy(client, **overrides):
    body = {"key": "refund-controls", "name": "Refund controls", "document": DOCUMENT}
    body.update(overrides)
    response = await client.post("/v1/governance/policies", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def test_creating_a_policy_returns_its_first_draft_and_audits(client, session):
    data = await _create_policy(client)
    assert data["active_version"] is None
    assert data["version"]["state"] == "DRAFT"
    assert data["version"]["editable"] is True
    assert data["version"]["allowed_transitions"] == ["ARCHIVED", "REVIEW"]
    session.expire_all()
    assert session.exec(
        select(AuditEvent).where(AuditEvent.action == "governance.policy_created")
    ).first()


async def test_an_author_cannot_approve_their_own_version(client):
    data = await _create_policy(client)
    await client.post(
        f"/v1/governance/policies/{data['id']}/versions/1/transition", json={"to": "REVIEW"}
    )
    response = await client.post(
        f"/v1/governance/policies/{data['id']}/versions/1/transition", json={"to": "APPROVED"}
    )
    assert response.status_code == 403
    assert "cannot approve it" in response.json()["error"]["message"]


async def test_an_impossible_transition_names_both_ends(client):
    data = await _create_policy(client)
    response = await client.post(
        f"/v1/governance/policies/{data['id']}/versions/1/transition", json={"to": "ACTIVE"}
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_transition"
    assert "DRAFT" in body["message"] and "REVIEW" in body["message"]


async def test_evaluate_explains_the_decision_and_changes_nothing(client, session):
    response = await client.post(
        "/v1/governance/evaluate",
        json={"integration_id": "payments", "tool_name": "refund_payment"},
    )
    data = response.json()["data"]
    assert [layer["layer"] for layer in data["layers"]] == ["tenant", "rbac", "abac"]
    assert data["evaluated_as"]["kind"] == "user"
    assert all(layer["reason"] for layer in data["layers"])


async def test_a_compliance_run_reports_manual_controls_separately(client):
    response = await client.post("/v1/governance/compliance/runs", json={"framework": "soc_2"})
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["state"] == "complete"
    assert data["manual"] > 0
    assert (
        data["controls"]
        == data["passed"] + data["failed"] + data["manual"] + data["not_applicable"]
    )
    assert "not a certification" in data["assessment_scope"]


async def test_a_simulated_timeout_is_recorded_as_a_timeout(client):
    response = await client.post(
        "/v1/governance/compliance/runs",
        json={"framework": "soc_2", "simulate_timeout": "the scanner did not answer"},
    )
    data = response.json()["data"]
    assert data["state"] == "timed_out"
    assert data["passed"] == 0
    assert "did not answer" in data["failure_reason"]


async def test_the_run_list_says_it_does_not_certify(client):
    await client.post("/v1/governance/compliance/runs", json={"framework": "gdpr"})
    response = await client.get("/v1/governance/compliance/runs")
    data = response.json()["data"]
    assert data["certifies"] is False
    assert len(data["runs"]) == 1


async def test_the_risk_endpoint_says_lower_is_better(client, session, test_org):
    from sutr.registry import service as registry_service

    tool = registry_service.register(
        session, org_id=test_org.id, tool_key="risk-api", name="Risk API"
    )
    session.commit()
    response = await client.get(f"/v1/governance/risk/{tool.id}")
    data = response.json()["data"]
    assert data["lower_is_better"] is True
    assert data["max"] == 100
    assert len(data["components"]) == 7


async def test_capabilities_reports_which_governance_points_are_gated(client):
    response = await client.get("/v1/governance/capabilities")
    data = response.json()["data"]
    assert data["points_total"] == 7
    assert data["points_gated"] == 6
    ungated = [point["point"] for point in data["points"] if not point["gated"]]
    assert ungated == ["monitoring"]
    assert "NOT IMPLEMENTED" in next(
        point["detail"] for point in data["points"] if point["point"] == "monitoring"
    )
    assert len(data["fail_safe"]) == 4
    assert data["compliance"]["certifies"] is False


async def test_an_exception_needs_a_justification_and_always_expires(client):
    response = await client.post(
        "/v1/governance/exceptions",
        json={
            "violation": "secrets are stored in plaintext",
            "justification": "Vault lands next sprint.",
            "control": "crypto.secrets_at_rest",
            "days": 14,
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["state"] == "requested"
    assert data["expires_at"]
    assert data["revalidate_at"] < data["expires_at"]

    listed = await client.get("/v1/governance/exceptions")
    assert listed.json()["data"]["all_expire"] is True


async def test_approving_an_exception_without_an_assessment_is_refused(client, session, test_user):
    other = User(email="decider@example.com", hashed_password="x", email_verified=True)
    session.add(other)
    session.commit()

    created = await client.post(
        "/v1/governance/exceptions",
        json={"violation": "v", "justification": "j", "days": 7},
    )
    exception_id = created.json()["data"]["id"]
    # The same person decides, which is refused first.
    response = await client.post(
        f"/v1/governance/exceptions/{exception_id}/decide", json={"approve": True}
    )
    assert response.status_code == 403
    assert "somebody else" in response.json()["error"]["message"]


async def test_writing_policy_needs_more_than_read_access(client, session, test_user):
    membership = session.exec(
        select(OrgMembership).where(OrgMembership.user_id == test_user.id)
    ).one()
    membership.role = "developer"
    session.add(membership)
    session.commit()
    response = await client.post(
        "/v1/governance/policies", json={"key": "nope", "name": "Nope", "document": DOCUMENT}
    )
    assert response.status_code == 403


# ── Cross-tenant ─────────────────────────────────────────────────────────────


class _OrgClient:
    def __init__(self, client, user, org):
        self._client, self._user, self._org = client, user, org

    async def _call(self, method, *args, **kwargs):
        from sutr.dependencies import AgentAuth, get_agent_auth
        from sutr.main import app

        previous = app.dependency_overrides.get(get_agent_auth)
        app.dependency_overrides[get_agent_auth] = lambda: AgentAuth(
            user=self._user, org=self._org, api_key=None
        )
        try:
            return await getattr(self._client, method)(*args, **kwargs)
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_agent_auth, None)
            else:
                app.dependency_overrides[get_agent_auth] = previous

    async def get(self, *args, **kwargs):
        return await self._call("get", *args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._call("post", *args, **kwargs)


async def test_another_tenant_sees_no_policies_and_gets_404(client, session, test_user):
    data = await _create_policy(client)
    await client.post("/v1/governance/compliance/runs", json={"framework": "soc_2"})

    org = Org(name="Intruder", slug="intruder-gov", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    intruder = _OrgClient(client, test_user, org)

    assert (await intruder.get("/v1/governance/policies")).json()["data"]["policies"] == []
    assert (await intruder.get("/v1/governance/compliance/runs")).json()["data"]["runs"] == []
    assert (await intruder.get(f"/v1/governance/policies/{data['id']}")).status_code == 404
    response = await intruder.post(
        f"/v1/governance/policies/{data['id']}/versions/1/transition", json={"to": "REVIEW"}
    )
    assert response.status_code == 404


async def test_a_missing_policy_is_404(client):
    assert (await client.get(f"/v1/governance/policies/{uuid.uuid4()}")).status_code == 404
