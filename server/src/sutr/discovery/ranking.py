"""The ranking function: configurable, versioned, and explainable.

LLD §3.8: *"Ranking function is configurable & versioned."* Both properties are
load-bearing for different reasons. Configurable, because what makes a good
recommendation differs between a marketplace optimising for adoption and a
regulated tenant optimising for trust. Versioned, because a ranking that changes
without a name is a ranking whose past results cannot be explained — and because
the version is part of the cache key, so changing it invalidates answers
computed under the old one rather than serving them forever.

A version is a set of weights over signals that are already computed by the time
ranking runs:

    relevance      the fused retrieval score — how well it matched the intent
    trust          the registry's 0–100 score, when there is one
    entitlement    the caller can already use it
    adoption       how many tenants subscribe
    freshness      recently published
    deprecation    a penalty, not a bonus

Two rules that are decisions rather than arithmetic.

**A missing trust score is neutral, not zero.** Phase 6 was careful to make an
unmeasurable trust score `null` rather than 0; ranking it as 0 here would undo
that in the one place it is most visible. An unscored tool ranks as if trust
were not a factor for it, and the explanation says the signal was absent.

**Every result carries its own arithmetic.** `explain` returns the contribution
of each signal, so "why is this first" is answerable without re-running the
query with a debugger attached.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sutr.discovery.corpus import Candidate, entitled

# Signals a version may weight. Named here so an unknown key in a weight map is
# a configuration error rather than a silently ignored line.
SIGNALS = ("relevance", "trust", "entitlement", "adoption", "freshness", "deprecation")

# How old a tool can be before freshness stops contributing.
FRESHNESS_DAYS = 90
# The subscriber count at which adoption saturates. Beyond it, more subscribers
# say nothing new — the difference between 0 and 10 is informative, the
# difference between 400 and 410 is not.
ADOPTION_SATURATION = 25


@dataclass(frozen=True)
class Version:
    name: str
    description: str
    weights: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "weights": self.weights}


VERSIONS: dict[str, Version] = {
    # The default. Relevance dominates — an intent search that returns a
    # popular, trusted tool that does not do what was asked is a worse answer
    # than a relevant one nobody has rated.
    "balanced-1": Version(
        name="balanced-1",
        description="Relevance first, then trust; adoption and freshness break ties.",
        weights={
            "relevance": 1.0,
            "trust": 0.35,
            "entitlement": 0.15,
            "adoption": 0.10,
            "freshness": 0.05,
            "deprecation": -0.30,
        },
    ),
    # For a caller who cares more about who they are calling than about the
    # closeness of the match.
    "trust-first-1": Version(
        name="trust-first-1",
        description="Trust weighted as heavily as relevance, for regulated callers.",
        weights={
            "relevance": 1.0,
            "trust": 1.0,
            "entitlement": 0.15,
            "adoption": 0.05,
            "freshness": 0.0,
            "deprecation": -0.50,
        },
    ),
    # Retrieval order and nothing else. This is what a ranking timeout falls
    # back to, and it exists as a named version so that fallback is a
    # configuration the platform already understands rather than a special case.
    "relevance-only-1": Version(
        name="relevance-only-1",
        description="Fused retrieval order alone; no trust, adoption or freshness.",
        weights={
            "relevance": 1.0,
            "trust": 0.0,
            "entitlement": 0.0,
            "adoption": 0.0,
            "freshness": 0.0,
            "deprecation": 0.0,
        },
    ),
}

DEFAULT_VERSION = "balanced-1"
FALLBACK_VERSION = "relevance-only-1"


@dataclass
class Ranked:
    candidate: Candidate
    score: float
    signals: dict[str, float | None]
    contributions: dict[str, float]
    matched_terms: list[str] = field(default_factory=list)

    def explain(self) -> str:
        parts = [
            f"{name} {value:+.3f}"
            for name, value in sorted(self.contributions.items(), key=lambda kv: -abs(kv[1]))
            if value
        ]
        absent = [name for name, value in self.signals.items() if value is None]
        sentence = f"{self.score:.3f} = " + (" ".join(parts) if parts else "no contributing signal")
        if absent:
            sentence += f". Absent: {', '.join(absent)}."
        return sentence

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.candidate.as_dict(),
            "score": round(self.score, 5),
            "matched_terms": self.matched_terms,
            "signals": {
                name: (round(value, 4) if value is not None else None)
                for name, value in self.signals.items()
            },
            "contributions": {name: round(value, 5) for name, value in self.contributions.items()},
            "explanation": self.explain(),
        }


def get_version(name: str | None) -> Version:
    return VERSIONS.get(name or DEFAULT_VERSION, VERSIONS[DEFAULT_VERSION])


def _freshness(candidate: Candidate, now: datetime) -> float | None:
    if candidate.published_at is None:
        return None
    age_days = (now - candidate.published_at).total_seconds() / 86400
    if age_days < 0:
        return 1.0
    return max(0.0, 1.0 - age_days / FRESHNESS_DAYS)


def signals_for(
    candidate: Candidate, relevance: float, *, now: datetime | None = None
) -> dict[str, float | None]:
    moment = now or datetime.now(timezone.utc)
    return {
        "relevance": relevance,
        # None, not 0.0: an unmeasured trust score must not read as a bad one.
        "trust": (candidate.trust_score / 100.0) if candidate.trust_score is not None else None,
        "entitlement": 1.0 if entitled(candidate) else 0.0,
        "adoption": min(candidate.subscriber_count, ADOPTION_SATURATION) / ADOPTION_SATURATION,
        "freshness": _freshness(candidate, moment),
        "deprecation": 1.0 if candidate.deprecated else 0.0,
    }


def rank(
    candidates: list[Candidate],
    *,
    relevance: dict[uuid.UUID, float],
    matched_terms: dict[uuid.UUID, list[str]] | None = None,
    version: str | None = None,
    now: datetime | None = None,
) -> tuple[list[Ranked], Version]:
    """Score and order. Highest first; ties broken by name for determinism."""
    chosen = get_version(version)
    matched = matched_terms or {}
    # Relevance is normalised across this result set, so a weight of 1.0 means
    # the same thing whatever scale the fusion happened to produce.
    highest = max(relevance.values(), default=0.0) or 1.0

    ranked: list[Ranked] = []
    for candidate in candidates:
        raw = relevance.get(candidate.tool_id, 0.0) / highest
        signals = signals_for(candidate, raw, now=now)
        contributions = {
            name: chosen.weights.get(name, 0.0) * value
            for name, value in signals.items()
            if value is not None
        }
        ranked.append(
            Ranked(
                candidate=candidate,
                score=sum(contributions.values()),
                signals=signals,
                contributions=contributions,
                matched_terms=matched.get(candidate.tool_id, []),
            )
        )
    ranked.sort(key=lambda entry: (-entry.score, entry.candidate.name.lower()))
    return ranked, chosen


def describe() -> dict[str, Any]:
    return {
        "default": DEFAULT_VERSION,
        "fallback_on_timeout": FALLBACK_VERSION,
        "signals": list(SIGNALS),
        "versions": [version.as_dict() for version in VERSIONS.values()],
        "notes": {
            "trust": "A tool with no trust score is ranked as if trust did not apply to it.",
            "relevance": "Normalised across the result set, so weights mean the same thing "
            "whatever scale fusion produced.",
        },
    }
