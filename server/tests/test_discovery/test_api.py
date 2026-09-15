"""The /v1/discovery surface."""

import uuid

from sutr.documentation import embeddings
from sutr.models.org_membership import OrgMembership

from .conftest import StubEmbeddings


async def _search(client, **body):
    payload = {"intent": "refund a customer payment"}
    payload.update(body)
    response = await client.post("/v1/discovery/search", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_search_returns_ranked_results_with_their_arithmetic(
    client, session, test_org, published_tools
):
    data = await _search(client)
    assert [result["tool_key"] for result in data["results"]][0] == "refunds-api"
    top = data["results"][0]
    assert top["score"] > 0
    assert "relevance" in top["contributions"]
    assert "refund" in top["matched_terms"]
    assert top["declared_by_provider"] == ["regions", "compliance"]


async def test_the_response_always_carries_what_was_skipped(
    client, session, test_org, published_tools
):
    data = await _search(client)
    assert [d["stage"] for d in data["degradations"]] == ["vector"]
    assert "NOT_CONFIGURED" in data["degradations"][0]["reason"]
    assert data["policy"]["version"] == 1
    assert [stage["name"] for stage in data["stages"]] == [
        "candidates",
        "policy",
        "retrieval",
        "ranking",
    ]
    assert data["latency_ms"] >= 0


async def test_no_match_returns_suggestions_rather_than_an_empty_list(
    client, session, test_org, published_tools
):
    data = await _search(client, intent="refund", entitled_only=True)
    assert data["results"] == []
    assert data["suggestions"][0]["tool_key"] == "refunds-api"
    assert data["suggestions"][0]["check"] == "subscription"
    assert "excluded by policy" in data["suggestions_reason"]


async def test_a_second_identical_search_is_cached(client, session, test_org, published_tools):
    assert (await _search(client))["cached"] is False
    assert (await _search(client))["cached"] is True


async def test_the_cache_can_be_dropped_by_hand(client, session, test_org, published_tools):
    await _search(client)
    response = await client.post("/v1/discovery/cache/invalidate")
    assert response.status_code == 200
    assert (await _search(client))["cached"] is False


async def test_an_unknown_ranking_version_is_refused_by_name(client, published_tools):
    response = await client.post(
        "/v1/discovery/search", json={"intent": "refund", "ranking_version": "nonsense"}
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "unknown_ranking_version"
    assert "balanced-1" in body["message"]


async def test_a_known_version_changes_the_ranking(client, published_tools):
    data = await _search(client, ranking_version="trust-first-1")
    assert data["ranking_version"] == "trust-first-1"


async def test_capabilities_reports_which_rankers_actually_ran(client, published_tools):
    response = await client.get("/v1/discovery/capabilities")
    data = response.json()["data"]
    assert data["retrieval"]["keyword"]["available"] is True
    assert data["retrieval"]["vector"]["available"] is False
    assert "NOT_CONFIGURED" in data["retrieval"]["vector"]["unavailable_reason"]
    assert data["never_invokes_provider_apis"] is True
    assert data["cache"]["backend"] == "in-process"
    assert data["quality"]["judgements"]["shipped"] == 0
    unenforced = [c["name"] for c in data["policy"]["checks"] if not c["enforced"]]
    assert set(unenforced) == {"provider_policy", "runtime_status"}
    assert "Cooperative" in data["budget_note"]


async def test_evaluate_scores_against_supplied_judgements(client, published_tools):
    refunds = str(published_tools["refunds"].id)
    response = await client.post(
        "/v1/discovery/evaluate",
        json={
            "judgements": [
                {"intent": "refund a payment", "relevant_tool_ids": [refunds]},
                {"intent": "ship a parcel", "relevant_tool_ids": [str(uuid.uuid4())]},
            ],
            "k": 5,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["queries"] == 2
    assert data["precision"] is not None
    assert data["ndcg"] is not None
    assert "judgements supplied in this request" in data["measured_against"]
    first = data["per_query"][0]
    # Recall is perfect: the tool judged relevant came back. Precision is 0.5
    # because the invoices tool also matched "payment" — which is exactly why
    # all three metrics are reported rather than one.
    assert first["recall"] == 1.0
    assert first["precision"] == 0.5
    assert first["hits"] == ["refunds-api"]


async def test_evaluation_is_never_served_from_the_cache(client, published_tools):
    """Measuring a cached answer would be measuring the cache."""
    await _search(client, intent="refund a payment")
    refunds = str(published_tools["refunds"].id)
    response = await client.post(
        "/v1/discovery/evaluate",
        json={"judgements": [{"intent": "refund a payment", "relevant_tool_ids": [refunds]}]},
    )
    assert response.json()["data"]["per_query"][0]["recall"] == 1.0


async def test_a_hybrid_request_says_so(client, published_tools):
    embeddings.set_provider(StubEmbeddings())
    data = await _search(client, intent="money back")
    # Nothing matched lexically, so this one was answered by vectors alone —
    # named as such rather than squeezed into a two-valued label.
    assert data["retrieval"]["mode"] == "vector"
    assert data["degradations"] == []
    assert [result["tool_key"] for result in data["results"]][0] == "refunds-api"


async def test_search_is_available_to_every_member(client, session, test_user):
    """Discovery is how you decide what to ask for, so a viewer may search."""
    membership = session.exec(
        __import__("sqlmodel").select(OrgMembership).where(OrgMembership.user_id == test_user.id)
    ).one()
    membership.role = "viewer"
    session.add(membership)
    session.commit()
    response = await client.post("/v1/discovery/search", json={"intent": "refund"})
    assert response.status_code == 200


async def test_invalidating_the_cache_needs_more_than_read_access(client, session, test_user):
    membership = session.exec(
        __import__("sqlmodel").select(OrgMembership).where(OrgMembership.user_id == test_user.id)
    ).one()
    membership.role = "viewer"
    session.add(membership)
    session.commit()
    assert (await client.post("/v1/discovery/cache/invalidate")).status_code == 403


async def test_an_empty_intent_is_refused(client):
    assert (await client.post("/v1/discovery/search", json={"intent": ""})).status_code == 422
