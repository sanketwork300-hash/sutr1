"""Cross-tenant isolation, systematically (build prompt §61, LLD §4.3.11).

Every phase has added its own negative tests. This suite is the one that does
not belong to a phase: it walks **every tenant-scoped `/v1` surface** and
asserts the same two properties for each, so a new resource added without
org-scoping fails here rather than in production.

The properties:

1. Another tenant's resource is **not listed**.
2. Reading it is **404, never 403** — a 403 confirms the resource exists, which
   turns an authorization boundary into an existence oracle.
"""

import uuid

import pytest
from sqlmodel import select

from sutr.models.access_pass import AccessPass
from sutr.models.access_rule import AccessRule
from sutr.models.agent_identity import AgentIdentity
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.provisioning import identity, pdp, rules


class _OrgClient:
    """A client bound to a second organization, one request at a time.

    The override is swapped in for the call and restored afterwards. A
    permanent one would silently turn every later request in the test into the
    second tenant's — the exact mistake this suite exists to catch.
    """

    def __init__(self, client, user, org):
        self._client = client
        self._user = user
        self._org = org

    async def _call(self, method: str, *args, **kwargs):
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

    async def patch(self, *args, **kwargs):
        return await self._call("patch", *args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._call("delete", *args, **kwargs)


@pytest.fixture(name="intruder")
async def intruder_fixture(client, session, test_user):
    org = Org(name="Intruder", slug="intruder-org", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return _OrgClient(client, test_user, org)


# ── The resources every phase added ──────────────────────────────────────────


async def _make_everything(client) -> dict[str, str]:
    """One resource of each tenant-scoped kind, created as the first tenant."""
    created: dict[str, str] = {}

    agent = await client.post(
        "/v1/provisioning/agents",
        json={"name": "their-agent", "attributes": {"role": "finance"}},
    )
    created["agent"] = agent.json()["data"]["id"]

    rule = await client.post(
        "/v1/provisioning/rules",
        json={"name": "their-rule", "effect": "deny", "subject": {"role": "finance"}},
    )
    created["rule"] = rule.json()["data"]["id"]

    tool = await client.post(
        "/v1/registry/tools", json={"tool_key": "their-tool", "name": "Theirs"}
    )
    created["registry_tool"] = tool.json()["data"]["id"]

    document = await client.post(
        "/v1/documentation/jobs",
        json={
            "content": "# Policy\n\nRefunds are allowed only within 30 days.",
            "filename": "p.md",
        },
    )
    created["document"] = document.json()["data"]["document"]["id"]

    return created


def _rows(payload, key):
    """The rows in a list response.

    Two shapes are in use: `/v1` collections added from Phase 3 onward wrap
    their rows under a name, while the documentation service returns the list
    itself. Both are read here rather than changing a shipped response — this
    suite is about isolation, not about tidying the envelope.
    """
    return payload if isinstance(payload, list) else payload[key]


async def test_no_tenant_scoped_resource_of_another_org_is_listed(client, intruder):
    await _make_everything(client)
    listings = {
        "/v1/provisioning/agents": "agents",
        "/v1/provisioning/rules": "rules",
        "/v1/provisioning/passes": "passes",
        "/v1/registry/tools": "tools",
        "/v1/documentation/documents": "documents",
        "/v1/generation/runtimes": "artifacts",
    }
    for path, key in listings.items():
        response = await intruder.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert _rows(response.json()["data"], key) == [], f"{path} leaked another tenant's rows"


async def test_reading_another_tenants_resource_is_404_never_403(client, intruder):
    """403 would confirm it exists. 404 says nothing either way."""
    created = await _make_everything(client)
    paths = [
        f"/v1/provisioning/agents/{created['agent']}",
        f"/v1/registry/tools/{created['registry_tool']}",
        f"/v1/registry/tools/{created['registry_tool']}/trust",
        f"/v1/registry/tools/{created['registry_tool']}/versions",
        f"/v1/documentation/documents/{created['document']}",
        f"/v1/documentation/documents/{created['document']}/chunks",
    ]
    for path in paths:
        response = await intruder.get(path)
        assert response.status_code == 404, f"{path} answered {response.status_code}"


async def test_writing_to_another_tenants_resource_is_404(client, intruder):
    created = await _make_everything(client)
    writes = [
        ("patch", f"/v1/provisioning/agents/{created['agent']}", {"description": "mine now"}),
        ("post", f"/v1/provisioning/agents/{created['agent']}/revoke", {}),
        (
            "post",
            f"/v1/registry/tools/{created['registry_tool']}/transition",
            {"to": "API_UPLOADED"},
        ),
        ("post", f"/v1/registry/tools/{created['registry_tool']}/archive", None),
        ("delete", f"/v1/provisioning/rules/{created['rule']}", None),
    ]
    for method, path, body in writes:
        response = await getattr(intruder, method)(
            path, **({"json": body} if body is not None else {})
        )
        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"


async def test_another_tenants_document_knowledge_never_appears(client, intruder):
    await _make_everything(client)
    for path, key in (
        ("/v1/documentation/rules", "rules"),
        ("/v1/documentation/glossary", "terms"),
        ("/v1/documentation/workflows", "workflows"),
    ):
        response = await intruder.get(path)
        assert _rows(response.json()["data"], key) == [], path
    search = await intruder.get("/v1/documentation/search", params={"q": "refunds"})
    assert search.json()["data"]["hits"] == []


async def test_another_tenants_tool_is_not_discoverable(client, intruder):
    await _make_everything(client)
    response = await intruder.post("/v1/discovery/search", json={"intent": "theirs"})
    assert response.json()["data"]["results"] == []
    assert response.json()["data"]["suggestions"] == []


# ── The decision layer itself ────────────────────────────────────────────────


def test_the_tenant_layer_refuses_before_anything_else_loads(session, test_org):
    """Layer 1 is first precisely so a foreign caller never reaches a rule set."""
    other = uuid.uuid4()
    principal = identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid.uuid4()),
        kind=identity.KIND_USER,
        org_id=other,
        role="owner",
    )
    decision = pdp.authorize(
        session,
        principal=principal,
        org_id=test_org.id,
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        permission="tools:execute",
    )
    assert decision.allowed is False
    assert decision.denied_by == "tenant"
    assert len(decision.layers) == 1


def test_an_owner_role_in_another_tenant_grants_nothing_here(session, test_org, other_org):
    """Privilege is per tenant. The highest role elsewhere is no role here."""
    principal = identity.Principal(
        urn=identity.urn(identity.KIND_USER, uuid.uuid4()),
        kind=identity.KIND_USER,
        org_id=other_org.id,
        role="owner",
    )
    decision = pdp.authorize(
        session,
        principal=principal,
        org_id=test_org.id,
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        permission="tools:execute",
    )
    assert decision.allowed is False


def test_every_row_written_by_this_phase_carries_its_tenant(session, test_org, agent, refund_rule):
    """The structural half: no table here has a row without an org."""
    for model in (AgentIdentity, AccessRule, AccessPass):
        for row in session.exec(select(model)).all():
            assert row.org_id == test_org.id


def test_an_agent_lookup_from_another_tenant_returns_nothing(session, test_org, other_org, agent):
    assert identity.get(session, agent.id, other_org.id) is None
    assert identity.get(session, agent.id, test_org.id) is not None
    assert identity.list_agents(session, org_id=other_org.id) == []


def test_a_rule_lookup_from_another_tenant_returns_nothing(
    session, test_org, other_org, refund_rule
):
    assert rules.get(session, refund_rule.id, other_org.id) is None
    assert rules.list_rules(session, org_id=other_org.id) == []
