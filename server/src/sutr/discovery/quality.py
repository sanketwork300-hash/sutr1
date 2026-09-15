"""Retrieval quality: precision, recall and NDCG.

LLD §3.8: *"Quality tracked via precision / recall / NDCG."* Those three are
computed here, and the honest part is what they are computed *against*.

All three need **relevance judgements** — somebody saying which tools should
have come back for a given intent. No such judgements ship with this platform,
and inventing them would produce metrics measuring a fiction. So the evaluation
is a function the operator feeds: supply intents and the tool ids you consider
relevant, and it runs real discovery queries and reports how it did.

That is the difference between "quality is tracked" and "a quality endpoint
exists". Nothing here reports a score until somebody says what a good answer is.

Definitions, since these are easy to state loosely:

- **precision@k** — of the first *k* results, the fraction that were relevant.
- **recall@k** — of the relevant tools, the fraction that appeared in the first
  *k*. Undefined when a judgement lists no relevant tools, and reported as
  `null` rather than as 1.0, which would flatter an empty query set.
- **NDCG@k** — discounted cumulative gain over binary relevance, normalised by
  the best possible ordering. It is the one of the three that notices *where* in
  the list a relevant result landed.
"""

import math
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_K = 10


@dataclass
class Judgement:
    """One intent and the tools a human considers right for it."""

    intent: str
    relevant: set[uuid.UUID]
    note: str = ""


@dataclass
class QueryScore:
    intent: str
    returned: int
    relevant: int
    precision: float
    recall: float | None
    ndcg: float
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "returned": self.returned,
            "relevant": self.relevant,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4) if self.recall is not None else None,
            "ndcg": round(self.ndcg, 4),
            "hits": self.hits,
            "misses": self.misses,
        }


@dataclass
class Evaluation:
    k: int
    queries: list[QueryScore]

    def _mean(self, attribute: str) -> float | None:
        values = [
            getattr(query, attribute)
            for query in self.queries
            if getattr(query, attribute) is not None
        ]
        return round(sum(values) / len(values), 4) if values else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "queries": len(self.queries),
            "precision": self._mean("precision"),
            "recall": self._mean("recall"),
            "ndcg": self._mean("ndcg"),
            "per_query": [query.as_dict() for query in self.queries],
            "measured_against": (
                "the judgements supplied in this request. No relevance judgements ship with the "
                "platform, so these numbers describe the set you provided and nothing else."
            ),
        }


def precision_at_k(returned: list[uuid.UUID], relevant: set[uuid.UUID], k: int) -> float:
    window = returned[:k]
    if not window:
        return 0.0
    return sum(1 for tool_id in window if tool_id in relevant) / len(window)


def recall_at_k(returned: list[uuid.UUID], relevant: set[uuid.UUID], k: int) -> float | None:
    if not relevant:
        # Nothing was judged relevant, so there is no recall to report. `None`
        # rather than 1.0: "found everything" and "there was nothing to find"
        # are different results.
        return None
    window = set(returned[:k])
    return len(window & relevant) / len(relevant)


def ndcg_at_k(returned: list[uuid.UUID], relevant: set[uuid.UUID], k: int) -> float:
    """Binary-relevance NDCG: position matters, and the ideal is all hits first."""
    if not relevant:
        return 0.0
    gains = [1.0 if tool_id in relevant else 0.0 for tool_id in returned[:k]]
    discounted = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal_hits = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
    return discounted / ideal if ideal else 0.0


def score_query(
    judgement: Judgement,
    returned: list[uuid.UUID],
    *,
    k: int = DEFAULT_K,
    names: dict[uuid.UUID, str] | None = None,
) -> QueryScore:
    labels = names or {}
    window = returned[:k]
    return QueryScore(
        intent=judgement.intent,
        returned=len(window),
        relevant=len(judgement.relevant),
        precision=precision_at_k(returned, judgement.relevant, k),
        recall=recall_at_k(returned, judgement.relevant, k),
        ndcg=ndcg_at_k(returned, judgement.relevant, k),
        hits=[labels.get(t, str(t)) for t in window if t in judgement.relevant],
        misses=[labels.get(t, str(t)) for t in judgement.relevant if t not in set(window)],
    )


def describe() -> dict[str, Any]:
    return {
        "metrics": ["precision@k", "recall@k", "ndcg@k"],
        "default_k": DEFAULT_K,
        "judgements": {
            "shipped": 0,
            "reason": (
                "NOT_CONFIGURED: no relevance judgements ship with this platform. Precision, "
                "recall and NDCG are computed against judgements supplied to "
                "POST /v1/discovery/evaluate; without them there is nothing to measure and no "
                "score is reported."
            ),
        },
    }
