"""Hybrid retrieval: keyword, vectors, graph — fused by reciprocal rank.

LLD §3.8: *"keyword for exact identifiers, vectors for meaning, graph for
business relationships"*. Three rankers, each good at something the others are
bad at, and none trusted alone.

- **Keyword** is BM25 over the candidate's own text (`common/lexical.py`). It is
  the one that finds `refunds-api` when somebody types `refunds-api`, which no
  amount of semantic similarity reliably does.
- **Vectors** find meaning: `issue money back` matching a tool that never says
  "refund". This needs an embedding provider, and none ships (ADR-033) — so the
  path is implemented against the provider interface, and without one the
  request degrades to lexical-only and says so, which is the LLD's own
  prescription for an embedding failure.
- **Graph** finds business relationships, using the knowledge graph the
  documentation service extracted. An intent mentioning "chargeback" expands to
  the terms the provider's own documents relate it to, and those expanded terms
  run a second lexical pass. When the graph is empty or unreachable, expansion
  is skipped and the request continues — the LLD's "graph DB down" row.

Fusion is Reciprocal Rank Fusion, which uses only the *rank* a candidate reached
in each list. That matters: a BM25 score and a cosine similarity have no common
unit, and normalising them against each other would be inventing a conversion.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, col, select

from sutr.common import lexical
from sutr.discovery.corpus import Candidate
from sutr.documentation import embeddings as embedding_module
from sutr.documentation.graph import normalize_key
from sutr.models.knowledge_graph import KnowledgeEdge, KnowledgeNode

logger = logging.getLogger(__name__)

LEXICAL = "keyword"
SEMANTIC = "vector"
GRAPH = "graph"

# How many terms a graph expansion may add. An unbounded expansion turns every
# query into a query for the whole vocabulary.
MAX_EXPANSION_TERMS = 12
# How many candidates the semantic ranker will embed in one request. A real
# install would precompute and store tool embeddings; this bound is what stops
# an unconfigured-but-present provider from being asked to embed a thousand
# tools synchronously on a latency-critical path.
MAX_SEMANTIC_CANDIDATES = 200

# Embeddings of candidate text, keyed by a hash of the text, for the lifetime of
# the process. Tool text changes rarely and a discovery request is not the place
# to re-embed it.
_VECTORS: dict[str, tuple[list[float], float]] = {}


@dataclass
class RankerResult:
    name: str
    ranking: list[uuid.UUID] = field(default_factory=list)
    # The ranker's own scores. Kept because fusion by rank alone flattens the
    # difference between a strong match and a weak one, which matters when this
    # is the only ranker that contributed — see `fuse`.
    scores: dict[uuid.UUID, float] = field(default_factory=dict)
    available: bool = True
    unavailable_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "hits": len(self.ranking),
            "detail": self.detail,
        }


@dataclass
class RetrievalResult:
    scores: dict[uuid.UUID, float]
    rankers: list[RankerResult]
    matched_terms: dict[uuid.UUID, list[str]]
    expansion: list[str]

    @property
    def contributing(self) -> list[str]:
        return [ranker.name for ranker in self.rankers if ranker.available and ranker.ranking]

    @property
    def mode(self) -> str:
        contributing = self.contributing
        if len(contributing) > 1:
            return "hybrid"
        return contributing[0] if contributing else "none"

    @property
    def degradations(self) -> list[dict[str, str]]:
        return [
            {"stage": ranker.name, "reason": ranker.unavailable_reason or "unavailable"}
            for ranker in self.rankers
            if not ranker.available
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rankers": [ranker.as_dict() for ranker in self.rankers],
            "expansion": self.expansion,
            "contributing": self.contributing,
            # Named by what actually contributed an ordering. A graph ranker
            # that found nothing to expand did not make this hybrid, and a
            # request answered by vectors alone is not "lexical" — a two-valued
            # label would have had to call it one or the other.
            "mode": self.mode,
        }


def reset_vector_cache() -> None:
    _VECTORS.clear()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def keyword(
    intent: str, candidates: list[Candidate]
) -> tuple[RankerResult, dict[uuid.UUID, list[str]]]:
    ranked = lexical.bm25(intent, [(c.tool_id, c.text()) for c in candidates])
    matched = {tool_id: terms for tool_id, _score, terms in ranked}
    return (
        RankerResult(
            name=LEXICAL,
            ranking=[tool_id for tool_id, _s, _t in ranked],
            scores={tool_id: score for tool_id, score, _t in ranked},
        ),
        matched,
    )


def expand(session: Session, *, org_id: uuid.UUID, intent: str) -> tuple[RankerResult, list[str]]:
    """Terms the provider's own documentation relates to this intent.

    Nodes are matched by normalized key, then walked one hop. One hop, not two:
    the second hop reaches things related to things you mentioned, which in a
    small graph is most of it.
    """
    terms = lexical.tokenize(intent)
    if not terms:
        return RankerResult(name=GRAPH, detail={"reason": "no searchable terms"}), []
    try:
        keys = {normalize_key(term) for term in terms}
        nodes = session.exec(
            select(KnowledgeNode)
            .where(KnowledgeNode.org_id == org_id)
            .where(col(KnowledgeNode.entity_key).in_(keys))
        ).all()
        if not nodes:
            return (
                RankerResult(name=GRAPH, detail={"seed_nodes": 0}),
                [],
            )
        seed_ids = [node.id for node in nodes]
        edges = session.exec(
            select(KnowledgeEdge)
            .where(KnowledgeEdge.org_id == org_id)
            .where(
                col(KnowledgeEdge.source_node_id).in_(seed_ids)
                | col(KnowledgeEdge.target_node_id).in_(seed_ids)
            )
            .limit(MAX_EXPANSION_TERMS * 4)
        ).all()
        reached = {edge.source_node_id for edge in edges} | {edge.target_node_id for edge in edges}
        reached -= set(seed_ids)
        if not reached:
            return RankerResult(name=GRAPH, detail={"seed_nodes": len(nodes), "reached": 0}), []
        related = session.exec(
            select(KnowledgeNode)
            .where(KnowledgeNode.org_id == org_id)
            .where(col(KnowledgeNode.id).in_(list(reached)))
        ).all()
    except Exception as exc:  # the LLD's "graph DB down"
        logger.warning("discovery graph expansion failed: %s", exc)
        return (
            RankerResult(
                name=GRAPH,
                available=False,
                unavailable_reason=f"The knowledge graph could not be read: {exc}",
            ),
            [],
        )

    expansion = sorted({(node.label or node.entity_key).lower() for node in related})[
        :MAX_EXPANSION_TERMS
    ]
    return (
        RankerResult(
            name=GRAPH,
            detail={"seed_nodes": len(nodes), "expansion_terms": len(expansion)},
        ),
        expansion,
    )


def graph_ranking(
    expansion: list[str], candidates: list[Candidate]
) -> tuple[list[uuid.UUID], dict[uuid.UUID, float]]:
    """A second lexical pass over the expanded terms."""
    if not expansion:
        return [], {}
    ranked = lexical.bm25(" ".join(expansion), [(c.tool_id, c.text()) for c in candidates])
    return (
        [tool_id for tool_id, _score, _terms in ranked],
        {tool_id: score for tool_id, score, _terms in ranked},
    )


async def semantic(intent: str, candidates: list[Candidate]) -> RankerResult:
    """Rank by meaning, when an embedding provider is configured."""
    provider = embedding_module.get_provider()
    available, reason = provider.available()
    if not available:
        return RankerResult(
            name=SEMANTIC,
            available=False,
            unavailable_reason=reason or embedding_module.NOT_CONFIGURED,
        )

    subset = candidates[:MAX_SEMANTIC_CANDIDATES]
    missing = [c for c in subset if _digest(c.text()) not in _VECTORS]
    model = ""
    try:
        if missing:
            embedded = await provider.embed([c.text() for c in missing])
            model = embedded.model
            for candidate, vector in zip(missing, embedded.vectors, strict=True):
                # The norm is precomputed once so cosine is a dot product per
                # candidate rather than a square root per candidate.
                _VECTORS[_digest(candidate.text())] = (list(vector), _norm(vector))
        query = await provider.embed([intent])
    except Exception as exc:  # the LLD's "embedding failure"
        logger.warning("discovery semantic retrieval failed: %s", exc)
        return RankerResult(
            name=SEMANTIC,
            available=False,
            unavailable_reason=f"The embedding provider failed: {exc}",
        )

    query_vector = query.vectors[0]
    query_norm = _norm(query_vector)
    scored: list[tuple[uuid.UUID, float]] = []
    for candidate in subset:
        stored = _VECTORS.get(_digest(candidate.text()))
        if stored is None:
            continue
        vector, norm = stored
        if len(vector) != len(query_vector):
            # A dimension mismatch means the vectors came from different
            # models. Skipping is right; averaging them would be arithmetic on
            # two different meanings.
            continue
        scored.append(
            (candidate.tool_id, embedding_module.cosine(query_vector, query_norm, vector, norm))
        )
    scored.sort(key=lambda entry: entry[1], reverse=True)
    return RankerResult(
        name=SEMANTIC,
        ranking=[tool_id for tool_id, _score in scored],
        scores=dict(scored),
        detail={"model": model or query.model, "scored": len(scored)},
    )


def _norm(vector: list[float]) -> float:
    return sum(value * value for value in vector) ** 0.5


async def retrieve(
    session: Session,
    *,
    org_id: uuid.UUID,
    intent: str,
    candidates: list[Candidate],
    use_graph: bool = True,
    use_semantic: bool = True,
) -> RetrievalResult:
    """Run the rankers that are available and fuse what they produce."""
    keyword_result, matched = keyword(intent, candidates)
    rankers = [keyword_result]

    expansion: list[str] = []
    if use_graph:
        graph_result, expansion = expand(session, org_id=org_id, intent=intent)
        graph_result.ranking, graph_result.scores = graph_ranking(expansion, candidates)
        rankers.append(graph_result)

    if use_semantic:
        semantic_result = await semantic(intent, candidates)
        rankers.append(semantic_result)

    return RetrievalResult(
        scores=fuse([r for r in rankers if r.available and r.ranking]),
        rankers=rankers,
        matched_terms=matched,
        expansion=expansion,
    )


def fuse(contributing: list[RankerResult]) -> dict[uuid.UUID, float]:
    """Combine the rankers that produced an ordering.

    With **two or more**, Reciprocal Rank Fusion: it uses only the rank a
    candidate reached in each list, which is what makes it safe to combine
    rankers whose scores have no common unit — a BM25 score and a cosine
    similarity are not convertible.

    With **one**, its own scores, normalised. RRF over a single list would throw
    away the very thing that list knows: it turns first-and-second into 1/61 and
    1/62, so a strong match and a weak one arrive at the ranker two thousandths
    apart and every later signal outvotes relevance. On the default install
    exactly one ranker contributes, so this is the common path, not the corner.
    """
    if not contributing:
        return {}
    if len(contributing) == 1:
        scores = contributing[0].scores
        highest = max(scores.values(), default=0.0) or 1.0
        return {key: value / highest for key, value in scores.items()}
    return lexical.reciprocal_rank_fusion([ranker.ranking for ranker in contributing])
