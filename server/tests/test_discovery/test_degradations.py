"""The four graceful degradations LLD §3.8 specifies, each on its own.

Build prompt §79 asks for a test per degradation. Each one here breaks exactly
one thing and asserts two properties: the request still returns an answer, and
the answer *says* what was skipped.
"""

from sqlmodel import select

from sutr.discovery import retrieval, service
from sutr.documentation import embeddings
from sutr.models.marketplace_listing import MarketplaceListing
from sutr.registry import service as registry_service

from .conftest import StubEmbeddings


def _degradation(result, stage):
    return next((d for d in result.degradations if d.stage == stage), None)


# ── Embedding failure → lexical-only ─────────────────────────────────────────


async def test_with_no_embedding_provider_the_request_is_lexical_only(
    session, test_org, published_tools
):
    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert result.results, "a lexical answer is still an answer"
    degraded = _degradation(result, "vector")
    assert degraded is not None
    assert "NOT_CONFIGURED" in degraded.reason
    assert degraded.effect == "This request was ranked on keyword matching alone."
    assert result.retrieval_summary["mode"] == "keyword"
    assert result.retrieval_summary["contributing"] == ["keyword"]


async def test_a_provider_that_throws_degrades_rather_than_failing(
    session, test_org, published_tools
):
    embeddings.set_provider(StubEmbeddings(fail=True))
    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert result.results
    degraded = _degradation(result, "vector")
    assert "the embedding service is down" in degraded.reason


async def test_a_working_provider_makes_the_request_hybrid(session, test_org, published_tools):
    embeddings.set_provider(StubEmbeddings())
    result = await service.search(
        session, org_id=test_org.id, intent="refund money back", use_cache=False
    )
    assert result.retrieval_summary["mode"] == "hybrid"
    assert _degradation(result, "vector") is None
    names = {ranker["name"]: ranker for ranker in result.retrieval_summary["rankers"]}
    assert names["vector"]["available"] is True
    assert names["vector"]["hits"] > 0


async def test_the_vector_ranker_finds_meaning_the_keyword_ranker_misses(
    session, test_org, published_tools
):
    """`money back` shares no term with any tool, but means "refund"."""
    lexical_only = await service.search(
        session, org_id=test_org.id, intent="money back", use_cache=False
    )
    assert lexical_only.results == []

    embeddings.set_provider(StubEmbeddings())
    hybrid = await service.search(session, org_id=test_org.id, intent="money back", use_cache=False)
    assert [e.candidate.tool_key for e in hybrid.results][0] == "refunds-api"


# ── Graph down → skip expansion, continue ────────────────────────────────────


async def test_a_graph_that_cannot_be_read_skips_expansion_and_continues(
    session, test_org, published_tools, monkeypatch
):
    def explode(*args, **kwargs):
        raise RuntimeError("the graph store is unreachable")

    monkeypatch.setattr(retrieval, "normalize_key", explode)
    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert result.results, "the request continues without the graph"
    degraded = _degradation(result, "graph")
    assert "could not be read" in degraded.reason
    assert degraded.effect == "Graph expansion was skipped."


async def test_an_empty_graph_is_not_a_degradation(session, test_org, published_tools):
    """Nothing to expand is normal; only an unreadable graph is a degradation."""
    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert _degradation(result, "graph") is None
    assert result.retrieval_summary["expansion"] == []


# ── Ranking timeout → retrieval order ────────────────────────────────────────


async def test_an_exhausted_budget_falls_back_to_retrieval_order(
    session, test_org, published_tools
):
    result = await service.search(
        session, org_id=test_org.id, intent="refund", use_cache=False, budget_ms=0
    )
    assert result.ranking_version == "relevance-only-1"
    degraded = _degradation(result, "ranking")
    assert "budget was spent before ranking" in degraded.reason
    assert degraded.effect == "Ordered by retrieval relevance alone."
    # And it is still an answer, ordered by how well it matched.
    assert [e.candidate.tool_key for e in result.results][0] == "refunds-api"


async def test_a_normal_budget_uses_the_requested_version(session, test_org, published_tools):
    result = await service.search(
        session,
        org_id=test_org.id,
        intent="refund",
        ranking_version="trust-first-1",
        use_cache=False,
    )
    assert result.ranking_version == "trust-first-1"
    assert _degradation(result, "ranking") is None


# ── Index lag → serve what is indexed, flag it ───────────────────────────────


async def test_a_listing_behind_its_record_is_served_and_flagged_stale(
    session, test_org, published_tools
):
    """The registry moved; the projection has not caught up."""
    registry_service.update(
        session, published_tools["refunds"], {"summary": "Refunds, now faster."}
    )
    session.commit()
    # Deliberately not drained: the listing is now behind the record.

    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert [e.candidate.tool_key for e in result.results] == ["refunds-api"]
    assert result.results[0].candidate.stale is True
    assert result.stale_results == 1
    degraded = _degradation(result, "index")
    assert "projected before their registry record last changed" in degraded.reason
    assert degraded.effect == "Served from the latest index and flagged stale."


async def test_a_caught_up_listing_is_not_flagged(session, test_org, published_tools):
    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert result.stale_results == 0
    assert _degradation(result, "index") is None
    assert session.exec(select(MarketplaceListing)).all()


async def test_the_degradation_list_is_empty_when_nothing_was_skipped(
    session, test_org, published_tools
):
    """An empty list is a claim, and it has to be true."""
    embeddings.set_provider(StubEmbeddings())
    result = await service.search(session, org_id=test_org.id, intent="refund", use_cache=False)
    assert result.degradations == []
    assert result.as_dict()["degradations"] == []
