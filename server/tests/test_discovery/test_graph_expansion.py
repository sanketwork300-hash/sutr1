"""Graph retrieval: business relationships from the documentation graph.

LLD §3.8 says the graph is there for *"business relationships"*. This is the
test that it does something a keyword ranker cannot: a search for a term the
tool never uses, which the provider's own documents relate to one it does.
"""

from sqlmodel import select

from sutr.discovery import retrieval, service
from sutr.models.knowledge_graph import KnowledgeEdge, KnowledgeNode


def _relate(session, org_id, left: str, right: str, relationship: str = "relates_to"):
    """Two nodes and an edge, the shape the documentation extractor produces."""
    nodes = []
    for term in (left, right):
        # Reused rather than recreated: the graph holds one node per (org,
        # type, key), which is what makes re-processing a document update it
        # instead of duplicating it.
        existing = session.exec(
            select(KnowledgeNode)
            .where(KnowledgeNode.org_id == org_id)
            .where(KnowledgeNode.entity_key == term.lower())
        ).first()
        node = existing or KnowledgeNode(
            org_id=org_id, entity_type="Term", entity_key=term.lower(), label=term
        )
        session.add(node)
        nodes.append(node)
    session.flush()
    session.add(
        KnowledgeEdge(
            org_id=org_id,
            source_node_id=nodes[0].id,
            target_node_id=nodes[1].id,
            relationship=relationship,
        )
    )
    session.commit()
    return nodes


async def test_the_graph_expands_an_intent_to_a_related_term(session, test_org, published_tools):
    """ "chargeback" appears in no tool; the graph says it relates to refunds."""
    before = await service.search(session, org_id=test_org.id, intent="chargeback", use_cache=False)
    assert before.results == []
    assert before.retrieval_summary["expansion"] == []

    _relate(session, test_org.id, "chargeback", "refunds")

    after = await service.search(session, org_id=test_org.id, intent="chargeback", use_cache=False)
    assert "refunds" in after.retrieval_summary["expansion"]
    assert [entry.candidate.tool_key for entry in after.results] == ["refunds-api"]
    assert after.retrieval_summary["mode"] == "graph"


async def test_expansion_is_scoped_to_the_tenants_own_graph(
    session, test_org, provider_org, published_tools
):
    """One tenant's documented vocabulary must not steer another's search."""
    _relate(session, provider_org.id, "chargeback", "refunds")
    result = await service.search(session, org_id=test_org.id, intent="chargeback", use_cache=False)
    assert result.retrieval_summary["expansion"] == []
    assert result.results == []


async def test_expansion_is_bounded(session, test_org, published_tools):
    for index in range(retrieval.MAX_EXPANSION_TERMS * 2):
        _relate(session, test_org.id, "chargeback", f"related-term-{index}")
    result = await service.search(session, org_id=test_org.id, intent="chargeback", use_cache=False)
    assert len(result.retrieval_summary["expansion"]) <= retrieval.MAX_EXPANSION_TERMS


async def test_an_intent_with_no_graph_match_expands_to_nothing(session, test_org, published_tools):
    _relate(session, test_org.id, "chargeback", "refunds")
    result = await service.search(session, org_id=test_org.id, intent="invoice", use_cache=False)
    assert result.retrieval_summary["expansion"] == []
    # And it still answers on keywords.
    assert [entry.candidate.tool_key for entry in result.results] == ["invoices-api"]


async def test_fusion_uses_rank_not_score(session, test_org, published_tools):
    """RRF combines orderings, so rankers on unrelated scales can be mixed."""
    from sutr.common import lexical

    fused = lexical.reciprocal_rank_fusion([["a", "b"], ["b", "a"]])
    assert fused["a"] == fused["b"], "same ranks in opposite orders cancel out"
    top = lexical.reciprocal_rank_fusion([["a", "b"], ["a", "c"]])
    assert top["a"] > top["b"] and top["a"] > top["c"]


def test_a_single_ranker_keeps_its_own_discrimination():
    """RRF over one list would flatten a strong match into a near-tie.

    First and second in a single ranking are 1/61 and 1/62 apart — two
    thousandths — so every later signal would outvote relevance. On the default
    install exactly one ranker contributes, which makes this the common path.
    """
    import uuid as uuid_module

    from sutr.discovery import retrieval as retrieval_module

    strong, weak = uuid_module.uuid4(), uuid_module.uuid4()
    single = retrieval_module.RankerResult(
        name="keyword", ranking=[strong, weak], scores={strong: 2.8, weak: 0.48}
    )
    fused = retrieval_module.fuse([single])
    assert fused[strong] == 1.0
    assert fused[weak] < 0.2, "the weak match stays weak"

    # With two rankers the units are no longer comparable, so rank fusion is
    # the right tool and the near-tie is the correct answer.
    second = retrieval_module.RankerResult(
        name="vector", ranking=[strong, weak], scores={strong: 0.9, weak: 0.89}
    )
    both = retrieval_module.fuse([single, second])
    assert both[strong] > both[weak]
    assert both[strong] - both[weak] < 0.01
