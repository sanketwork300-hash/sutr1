"""The /v1/registry and /v1/marketplace surfaces, including cross-tenant negatives."""

import uuid

from sqlmodel import select

from sutr.events import relay
from sutr.models.audit_event import AuditEvent
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User

PIPELINE_TO_DEPLOYED = (
    "API_UPLOADED",
    "TRANSLATING",
    "IR_READY",
    "METADATA_READY",
    "GENERATING_MCP",
    "VALIDATING",
    "DEPLOYING",
    "DEPLOYED",
)


async def _register(client, **overrides) -> dict:
    body = {
        "tool_key": "refunds-api",
        "name": "Refunds API",
        "summary": "Issue and track refunds.",
        "category": "Finance",
        "tags": ["payments"],
        "regions": ["eu-west-1"],
        "compliance": ["SOC2"],
    }
    body.update(overrides)
    response = await client.post("/v1/registry/tools", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _advance(client, tool_id: str, targets=PIPELINE_TO_DEPLOYED) -> None:
    for target in targets:
        response = await client.post(
            f"/v1/registry/tools/{tool_id}/transition", json={"to": target}
        )
        assert response.status_code == 200, response.text


async def _publish(client, tool_id: str) -> None:
    """Drive to PUBLISHED through both gates, deciding each one."""
    await _advance(client, tool_id)
    visibility = await client.post(
        f"/v1/registry/tools/{tool_id}/visibility", json={"visibility": "public"}
    )
    await client.post(
        f"/v1/registry/change-requests/{visibility.json()['data']['id']}/decide",
        json={"approve": True},
    )
    review = await client.post(
        f"/v1/registry/tools/{tool_id}/transition", json={"to": "UNDER_REVIEW"}
    )
    await client.post(
        f"/v1/registry/change-requests/{review.json()['data']['change_request_id']}/decide",
        json={"approve": True},
    )
    await client.post(f"/v1/registry/tools/{tool_id}/transition", json={"to": "APPROVED"})
    publish = await client.post(
        f"/v1/registry/tools/{tool_id}/transition", json={"to": "PUBLISHED"}
    )
    response = await client.post(
        f"/v1/registry/change-requests/{publish.json()['data']['change_request_id']}/decide",
        json={"approve": True},
    )
    assert response.status_code == 200, response.text


# ── Registry ─────────────────────────────────────────────────────────────────


async def test_registering_returns_the_record_and_audits(client, session):
    data = await _register(client)
    assert data["lifecycle_state"] == "DRAFT"
    assert data["visibility"] == "private"
    assert data["declared_by_provider"] == ["regions", "compliance"]
    assert data["trust"]["score"] is not None
    assert data["allowed_transitions"] == [{"to": "API_UPLOADED", "requires_approval": False}]

    session.expire_all()
    event = session.exec(
        select(AuditEvent).where(AuditEvent.action == "registry.tool_registered")
    ).one()
    assert event.target_id == data["id"]


async def test_a_duplicate_tool_key_is_a_conflict(client):
    await _register(client)
    response = await client.post(
        "/v1/registry/tools", json={"tool_key": "refunds-api", "name": "Again"}
    )
    assert response.status_code == 409


async def test_the_lifecycle_is_reported_so_clients_need_not_hard_code_it(client):
    response = await client.get("/v1/registry/lifecycle")
    data = response.json()["data"]
    assert len(data["pipeline"]) == 14
    assert {"from": "APPROVED", "to": "PUBLISHED"} in data["gated_transitions"]


async def test_an_impossible_transition_is_refused_with_both_ends_named(client):
    data = await _register(client)
    response = await client.post(
        f"/v1/registry/tools/{data['id']}/transition", json={"to": "PUBLISHED"}
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "invalid_transition"
    assert "DRAFT" in body["message"] and "PUBLISHED" in body["message"]


async def test_a_gated_transition_returns_a_change_request_and_applies_nothing(client):
    data = await _register(client)
    await _advance(client, data["id"])
    response = await client.post(
        f"/v1/registry/tools/{data['id']}/transition",
        json={"to": "UNDER_REVIEW", "reason": "ready"},
    )
    body = response.json()["data"]
    assert body["applied"] is False
    assert body["change_request_id"]
    assert "governance approval" in body["pending_reason"]
    assert body["tool"]["lifecycle_state"] == "DEPLOYED"


async def test_deciding_a_change_applies_it_and_audits(client, session):
    data = await _register(client)
    await _advance(client, data["id"])
    request = await client.post(
        f"/v1/registry/tools/{data['id']}/transition", json={"to": "UNDER_REVIEW"}
    )
    request_id = request.json()["data"]["change_request_id"]
    response = await client.post(
        f"/v1/registry/change-requests/{request_id}/decide", json={"approve": True}
    )
    assert response.json()["data"]["status"] == "approved"

    tool = await client.get(f"/v1/registry/tools/{data['id']}")
    assert tool.json()["data"]["lifecycle_state"] == "UNDER_REVIEW"
    session.expire_all()
    assert session.exec(
        select(AuditEvent).where(AuditEvent.action == "registry.change_decided")
    ).first()


async def test_a_second_admin_must_decide_when_one_exists(client, session, test_user, test_org):
    other = User(email="second@example.com", hashed_password="x", email_verified=True)
    session.add(other)
    session.flush()
    session.add(OrgMembership(user_id=other.id, org_id=test_org.id, role="admin"))
    session.commit()

    data = await _register(client)
    request = await client.post(
        f"/v1/registry/tools/{data['id']}/visibility", json={"visibility": "public"}
    )
    response = await client.post(
        f"/v1/registry/change-requests/{request.json()['data']['id']}/decide",
        json={"approve": True},
    )
    assert response.status_code == 403
    assert "somebody else" in response.json()["error"]["message"]


async def test_versions_are_listed_newest_first(client):
    data = await _register(client)
    for note in ("first", "second"):
        response = await client.post(
            f"/v1/registry/tools/{data['id']}/versions", json={"notes": note}
        )
        assert response.status_code == 201
    listed = await client.get(f"/v1/registry/tools/{data['id']}/versions")
    assert [v["version"] for v in listed.json()["data"]["versions"]] == [2, 1]

    detail = await client.get(f"/v1/registry/tools/{data['id']}/versions/1")
    assert detail.json()["data"]["notes"] == "first"
    assert "deployment_manifest" in detail.json()["data"]


async def test_pricing_shows_the_live_price_and_the_history(client):
    data = await _register(client)
    first = await client.post(
        f"/v1/registry/tools/{data['id']}/pricing",
        json={"model": "per_call", "amount_micros": 1000},
    )
    await client.post(
        f"/v1/registry/change-requests/{first.json()['data']['id']}/decide",
        json={"approve": True},
    )
    second = await client.post(
        f"/v1/registry/tools/{data['id']}/pricing",
        json={"model": "per_call", "amount_micros": 4000},
    )
    await client.post(
        f"/v1/registry/change-requests/{second.json()['data']['id']}/decide",
        json={"approve": True},
    )

    response = await client.get(f"/v1/registry/tools/{data['id']}/pricing")
    body = response.json()["data"]
    assert body["current"]["amount_micros"] == 4000
    assert len(body["history"]) == 2
    assert body["current"]["billed_by_this_platform"] is False


async def test_an_invalid_price_is_refused_at_request_time(client):
    data = await _register(client)
    response = await client.post(
        f"/v1/registry/tools/{data['id']}/pricing",
        json={"model": "per_call", "amount_micros": 0},
    )
    assert response.status_code == 400
    assert "needs an amount" in response.json()["error"]["message"]


async def test_the_trust_endpoint_explains_the_score(client):
    data = await _register(client)
    response = await client.get(f"/v1/registry/tools/{data['id']}/trust")
    body = response.json()["data"]
    assert body["max"] == 100
    assert len(body["components"]) == 7
    assert "Not measured:" in body["explanation"]
    unavailable = [c for c in body["components"] if not c["available"]]
    assert all(c["unavailable_reason"] for c in unavailable)


async def test_registering_requires_permission(client, session, test_user):
    membership = session.exec(
        select(OrgMembership).where(OrgMembership.user_id == test_user.id)
    ).one()
    membership.role = "member"
    session.add(membership)
    session.commit()
    response = await client.post(
        "/v1/registry/tools", json={"tool_key": "nope-key", "name": "Nope"}
    )
    assert response.status_code == 403


# ── Marketplace ──────────────────────────────────────────────────────────────


async def test_a_published_tool_appears_in_the_storefront_after_delivery(client):
    data = await _register(client)
    await _publish(client, data["id"])

    before = await client.get("/v1/marketplace/listings")
    assert before.json()["data"]["listings"] == []

    relay.drain_once()
    after = await client.get("/v1/marketplace/listings")
    listings = after.json()["data"]["listings"]
    assert [listing["tool_key"] for listing in listings] == ["refunds-api"]
    assert listings[0]["projected_from"] == "tool.published"
    assert "can lag" in after.json()["data"]["projection"]["note"]


async def test_a_listing_detail_carries_versions_and_the_trust_explanation(client):
    data = await _register(client)
    await client.post(f"/v1/registry/tools/{data['id']}/versions", json={"notes": "v1"})
    await _publish(client, data["id"])
    relay.drain_once()

    response = await client.get(f"/v1/marketplace/listings/{data['id']}")
    body = response.json()["data"]
    assert body["versions"]
    assert body["trust"]["components"]
    assert body["subscription"] is None


async def test_subscribing_and_cancelling_through_the_api(client, session, test_user):
    """A second tenant subscribes to the first tenant's published tool."""
    data = await _register(client)
    await _publish(client, data["id"])
    relay.drain_once()

    consumer_client = await _as_other_org(client, session, test_user)
    response = await consumer_client.post(
        "/v1/marketplace/subscriptions", json={"tool_id": data["id"]}
    )
    assert response.status_code == 201, response.text
    subscription = response.json()["data"]
    assert subscription["state"] == "subscribed"
    assert subscription["entitled"] is False

    provisioned = await consumer_client.post(
        f"/v1/marketplace/subscriptions/{subscription['id']}/provision", json={}
    )
    assert provisioned.json()["data"]["entitled"] is True

    cancelled = await consumer_client.post(
        f"/v1/marketplace/subscriptions/{subscription['id']}/cancel",
        json={"reason": "done"},
    )
    assert cancelled.json()["data"]["state"] == "cancelled"

    # The provider sees the subscription against their own tool.
    subscribers = await client.get("/v1/marketplace/subscribers")
    assert len(subscribers.json()["data"]["subscribers"]) == 1


async def test_a_provider_profile_cannot_verify_itself(client):
    response = await client.put(
        "/v1/marketplace/profile",
        json={"display_name": "Acme", "summary": "We refund.", "verified": True},
    )
    assert response.status_code == 200
    profile = response.json()["data"]["profile"]
    assert profile["display_name"] == "Acme"
    assert profile["verified"] is False
    assert profile["verification_is_platform_granted"] is True


async def test_the_provider_page_lists_their_published_tools(client, session, test_org):
    data = await _register(client)
    await _publish(client, data["id"])
    relay.drain_once()
    response = await client.get(f"/v1/marketplace/providers/{test_org.id}")
    body = response.json()["data"]
    assert [listing["tool_key"] for listing in body["listings"]] == ["refunds-api"]
    assert body["provider"]["display_name"] == test_org.name


# ── Cross-tenant negatives (build prompt §61) ───────────────────────────────


class _OrgClient:
    """A client bound to a second organization, one request at a time.

    The auth override is swapped in for the call and restored afterwards. A
    permanent override would silently turn every *later* request in the same
    test into the second tenant's, which is exactly the kind of mistake a
    cross-tenant test exists to catch and would instead be committing.
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


async def _as_other_org(client, session, test_user) -> _OrgClient:
    """A client authenticated into a second organization owned by the same user."""
    org = Org(name="Consumer", slug="consumer-org", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return _OrgClient(client, test_user, org)


async def test_another_tenants_tool_reads_as_404(client, session, test_user):
    data = await _register(client)
    other = await _as_other_org(client, session, test_user)
    for suffix in ("", "/trust", "/versions", "/pricing"):
        response = await other.get(f"/v1/registry/tools/{data['id']}{suffix}")
        assert response.status_code == 404, suffix


async def test_another_tenants_tools_are_not_listed(client, session, test_user):
    await _register(client)
    other = await _as_other_org(client, session, test_user)
    assert (await other.get("/v1/registry/tools")).json()["data"]["tools"] == []


async def test_another_tenants_change_request_cannot_be_decided(client, session, test_user):
    data = await _register(client)
    request = await client.post(
        f"/v1/registry/tools/{data['id']}/visibility", json={"visibility": "public"}
    )
    request_id = request.json()["data"]["id"]
    other = await _as_other_org(client, session, test_user)
    response = await other.post(
        f"/v1/registry/change-requests/{request_id}/decide", json={"approve": True}
    )
    assert response.status_code == 404


async def test_an_unpublished_tool_is_invisible_in_the_storefront(client, session, test_user):
    data = await _register(client)
    relay.drain_once()
    other = await _as_other_org(client, session, test_user)
    assert (await other.get("/v1/marketplace/listings")).json()["data"]["listings"] == []
    assert (await other.get(f"/v1/marketplace/listings/{data['id']}")).status_code == 404


async def test_a_private_tool_cannot_be_subscribed_to_and_reads_as_missing(
    client, session, test_user
):
    """404 rather than 403: a 403 would confirm the tool exists."""
    data = await _register(client)
    await _advance(client, data["id"])
    other = await _as_other_org(client, session, test_user)
    response = await other.post("/v1/marketplace/subscriptions", json={"tool_id": data["id"]})
    assert response.status_code == 404


async def test_an_organization_visible_tool_is_hidden_from_other_tenants(
    client, session, test_user
):
    data = await _register(client)
    await _advance(client, data["id"])
    request = await client.post(
        f"/v1/registry/tools/{data['id']}/visibility", json={"visibility": "organization"}
    )
    await client.post(
        f"/v1/registry/change-requests/{request.json()['data']['id']}/decide",
        json={"approve": True},
    )
    review = await client.post(
        f"/v1/registry/tools/{data['id']}/transition", json={"to": "UNDER_REVIEW"}
    )
    await client.post(
        f"/v1/registry/change-requests/{review.json()['data']['change_request_id']}/decide",
        json={"approve": True},
    )
    await client.post(f"/v1/registry/tools/{data['id']}/transition", json={"to": "APPROVED"})
    publish = await client.post(
        f"/v1/registry/tools/{data['id']}/transition", json={"to": "PUBLISHED"}
    )
    await client.post(
        f"/v1/registry/change-requests/{publish.json()['data']['change_request_id']}/decide",
        json={"approve": True},
    )
    relay.drain_once()

    # The owner sees it.
    assert (await client.get("/v1/marketplace/listings")).json()["data"]["listings"]
    # Nobody else does.
    other = await _as_other_org(client, session, test_user)
    assert (await other.get("/v1/marketplace/listings")).json()["data"]["listings"] == []


async def test_a_missing_tool_is_404(client):
    response = await client.get(f"/v1/registry/tools/{uuid.uuid4()}")
    assert response.status_code == 404
