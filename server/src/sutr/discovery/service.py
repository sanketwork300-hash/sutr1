"""The discovery request: stages, a latency budget, and four degradations.

    cache → candidates → policy filter → retrieval → ranking → cache

LLD §3.8 gives a latency budget and four ways to miss it gracefully. All four
are implemented, and each is a *named* degradation on the response rather than a
silent difference in behaviour:

| the LLD's row      | what happens here                                        |
|--------------------|----------------------------------------------------------|
| Graph DB down      | expansion is skipped, the request continues              |
| Embedding failure  | lexical-only for this request                            |
| Ranking timeout    | fall back to `relevance-only-1` — retrieval order        |
| Index lag          | serve what is indexed, flag the stale rows               |

The budget is **cooperative**: elapsed time is checked between stages, not
enforced by preemption. A stage that blocks for a minute will still take a
minute. That is a real limitation and is stated here rather than implied by the
word "budget" — enforcing it properly needs each stage to be cancellable, and
the two that could actually be slow (the embedding provider and the database)
are both awaited calls this code does not own.

The response always carries `degradations`. An empty list is a claim that
nothing was skipped, which is exactly as useful as the non-empty case.
"""

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from sqlmodel import Session

from sutr.common import lexical
from sutr.config import settings
from sutr.discovery import cache, corpus, policy, quality, ranking, retrieval
from sutr.discovery.policy import Requirements
from sutr.observability.tracing import span

# The LLD's target for a discovery search is under 500 ms (cover, §5.8). It is a
# design target and is NOT TESTED as a latency claim; what this constant does is
# decide when to stop spending time on ranking, which is checkable.
DEFAULT_BUDGET_MS = 500
# The slice of the budget ranking may use before it falls back. Ranking is the
# last stage, so it gets whatever is left, floored here so it is never asked to
# work in no time at all.
RANKING_FLOOR_MS = 20

# The number of suggestions returned when nothing survives the filter. LLD §4.2:
# "suggestions or 'tool not found'".
MAX_SUGGESTIONS = 5


@dataclass
class Stage:
    name: str
    duration_ms: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "duration_ms": round(self.duration_ms, 2), "detail": self.detail}


@dataclass
class Degradation:
    stage: str
    reason: str
    effect: str

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "reason": self.reason, "effect": self.effect}


@dataclass
class DiscoveryResult:
    intent: str
    results: list[ranking.Ranked]
    suggestions: list[policy.Exclusion]
    stages: list[Stage]
    degradations: list[Degradation]
    filter_summary: dict[str, Any]
    retrieval_summary: dict[str, Any]
    ranking_version: str
    cached: bool = False
    stale_results: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "results": [result.as_dict() for result in self.results],
            "suggestions": [suggestion.as_dict() for suggestion in self.suggestions],
            "suggestions_reason": (
                "Nothing matched that you are allowed to use. These matched your intent and were "
                "excluded by policy; the reason says which check and why."
                if self.suggestions
                else None
            ),
            "policy": self.filter_summary,
            "retrieval": self.retrieval_summary,
            "ranking_version": self.ranking_version,
            "degradations": [degradation.as_dict() for degradation in self.degradations],
            "stale_results": self.stale_results,
            "cached": self.cached,
            "stages": [stage.as_dict() for stage in self.stages],
            "latency_ms": round(sum(stage.duration_ms for stage in self.stages), 2),
        }


class _Clock:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last = self.started

    def lap(self) -> float:
        now = time.monotonic()
        elapsed = (now - self.last) * 1000
        self.last = now
        return elapsed

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000


async def search(
    session: Session,
    *,
    org_id: uuid.UUID,
    intent: str,
    requirements: Requirements | None = None,
    ranking_version: str | None = None,
    limit: int = 10,
    use_cache: bool = True,
    budget_ms: int | None = None,
) -> DiscoveryResult:
    """One discovery request, end to end, inside the Discovery trace span."""
    with span(
        "sutr.discovery.search",
        **{"sutr.stage": "discovery", "sutr.intent_length": len(intent)},
    ) as active_span:
        result = await _search(
            session,
            org_id=org_id,
            intent=intent,
            requirements=requirements,
            ranking_version=ranking_version,
            limit=limit,
            use_cache=use_cache,
            budget_ms=budget_ms,
        )
        if active_span is not None:
            _describe(active_span, result)
        return result


async def _search(
    session: Session,
    *,
    org_id: uuid.UUID,
    intent: str,
    requirements: Requirements | None = None,
    ranking_version: str | None = None,
    limit: int = 10,
    use_cache: bool = True,
    budget_ms: int | None = None,
) -> DiscoveryResult:
    """The request itself."""
    needs = requirements or Requirements()
    chosen_version = ranking.get_version(ranking_version)
    # `or` would turn a deliberate budget of 0 — the way a caller asks for the
    # timeout path — back into the default.
    budget = (
        budget_ms
        if budget_ms is not None
        else getattr(settings, "discovery_budget_ms", DEFAULT_BUDGET_MS)
    )

    cache_key = cache.key(
        org_id=org_id,
        intent=intent,
        policy_version=policy.POLICY_VERSION,
        ranking_version=chosen_version.name,
        requirements=needs.as_dict(),
        limit=limit,
    )
    if use_cache:
        hit = cache.get(cache_key, org_id)
        if hit is not None:
            # A copy, not the stored object: marking the cached one would
            # mutate every earlier caller's result, and the second reader would
            # change what the first one is holding.
            return replace(hit, cached=True)

    clock = _Clock()
    stages: list[Stage] = []
    degradations: list[Degradation] = []

    candidates = corpus.collect(session, org_id=org_id)
    stages.append(Stage("candidates", clock.lap(), {"count": len(candidates)}))

    # The filter runs *before* retrieval and ranking, as the LLD requires.
    filtered = policy.apply(candidates, org_id=org_id, requirements=needs)
    stages.append(Stage("policy", clock.lap(), filtered.as_dict()))

    retrieved = await retrieval.retrieve(
        session, org_id=org_id, intent=intent, candidates=filtered.allowed
    )
    stages.append(Stage("retrieval", clock.lap(), retrieved.as_dict()))
    for ranker in retrieved.rankers:
        if ranker.available:
            continue
        degradations.append(
            Degradation(
                stage=ranker.name,
                reason=ranker.unavailable_reason or "unavailable",
                effect=(
                    "Graph expansion was skipped."
                    if ranker.name == retrieval.GRAPH
                    else "This request was ranked on keyword matching alone."
                ),
            )
        )

    # Ranking timeout: fall back to retrieval order, which is a named version
    # rather than a special case.
    remaining = budget - clock.elapsed_ms
    version_to_use = chosen_version.name
    if remaining < RANKING_FLOOR_MS:
        version_to_use = ranking.FALLBACK_VERSION
        degradations.append(
            Degradation(
                stage="ranking",
                reason=(
                    f"The {budget:.0f}ms budget was spent before ranking "
                    f"({clock.elapsed_ms:.0f}ms elapsed)."
                ),
                effect="Ordered by retrieval relevance alone.",
            )
        )

    matching = [c for c in filtered.allowed if c.tool_id in retrieved.scores]
    ranked, used = ranking.rank(
        matching,
        relevance=retrieved.scores,
        matched_terms=retrieved.matched_terms,
        version=version_to_use,
    )
    stages.append(Stage("ranking", clock.lap(), {"version": used.name, "ranked": len(ranked)}))

    stale = [entry for entry in ranked[:limit] if entry.candidate.stale]
    if stale:
        degradations.append(
            Degradation(
                stage="index",
                reason=(
                    f"{len(stale)} listing(s) were projected before their registry record last "
                    "changed."
                ),
                effect="Served from the latest index and flagged stale.",
            )
        )

    result = DiscoveryResult(
        intent=intent,
        results=ranked[:limit],
        suggestions=_suggestions(intent, ranked, filtered),
        stages=stages,
        degradations=degradations,
        filter_summary=filtered.as_dict(),
        retrieval_summary=retrieved.as_dict(),
        ranking_version=used.name,
        stale_results=len(stale),
    )
    if use_cache:
        cache.put(cache_key, org_id, result)
    return result


def _describe(active_span, result: DiscoveryResult) -> None:
    """Put the shape of a discovery result on its span — never the content.

    How many candidates survived the policy filter, which ranking version ran,
    how long each stage took and which degradations fired are what an
    investigation needs. The intent itself is a user's sentence and can contain
    anything, so it never leaves the process on a span; only its length does,
    which is already set when the span opens.
    """
    active_span.set_attribute("sutr.ranking_version", result.ranking_version)
    active_span.set_attribute("sutr.result_count", len(result.results))
    active_span.set_attribute("sutr.stale_results", result.stale_results)
    active_span.set_attribute("sutr.cached", result.cached)
    active_span.set_attribute("sutr.degraded", bool(result.degradations))
    for stage in result.stages:
        active_span.set_attribute(f"sutr.stage.{stage.name}_ms", round(stage.duration_ms, 2))
    for degradation in result.degradations:
        active_span.add_event(
            "degraded",
            {"sutr.stage": degradation.stage, "sutr.effect": degradation.effect},
        )


def _suggestions(
    intent: str, ranked: list[ranking.Ranked], filtered: policy.FilterResult
) -> list[policy.Exclusion]:
    """What to say when nothing matched (LLD §4.2).

    Only when there are no results: an agent with an answer does not need to be
    told what it could not have. The suggestions are ranked by how well they
    matched the intent, so the first one is the tool the caller most likely
    meant — and each carries the check that excluded it, which is the
    actionable part.
    """
    if ranked or not filtered.excluded:
        return []
    scored = {
        key: score
        for key, score, _terms in lexical.bm25(
            intent, [(e.candidate.tool_id, e.candidate.text()) for e in filtered.excluded]
        )
    }
    matching = [e for e in filtered.excluded if e.candidate.tool_id in scored]
    matching.sort(key=lambda exclusion: -scored[exclusion.candidate.tool_id])
    return matching[:MAX_SUGGESTIONS]


def describe() -> dict[str, Any]:
    """What this install's discovery can and cannot do."""
    from sutr.documentation import embeddings as embedding_module

    available, reason = embedding_module.get_provider().available()
    return {
        "budget_ms": getattr(settings, "discovery_budget_ms", DEFAULT_BUDGET_MS),
        "budget_note": (
            "Cooperative: elapsed time is checked between stages, not enforced by preemption. A "
            "stage that blocks will still block."
        ),
        "retrieval": {
            "keyword": {"available": True, "unavailable_reason": None},
            "vector": {"available": available, "unavailable_reason": reason},
            "graph": {
                "available": True,
                "unavailable_reason": None,
                "detail": "Expands the intent through the documentation knowledge graph.",
            },
        },
        "policy": policy.describe(),
        "ranking": ranking.describe(),
        "cache": cache.describe(),
        "quality": quality.describe(),
        "never_invokes_provider_apis": True,
    }
