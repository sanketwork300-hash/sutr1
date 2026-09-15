"""The discovery query cache.

LLD §3.8 specifies the key and the invalidation:

    Query cache key = tenant + intent + policy version; invalidated on tool
    publish/update, policy change, runtime unavailable.

The key here is that, plus the ranking version and the caller's requirements —
both change the answer, and a cache that returned a `trust-first` ranking to a
caller who asked for `balanced` would be serving a different question's answer.

**Invalidation is by generation, not by deletion.** Each tenant holds a counter;
a registry event bumps it, and every key minted before the bump stops matching.
Deleting the affected entries instead would mean knowing which intents a tool
could have matched, which is the search problem again.

**The cache is in-process.** Each API replica has its own, which is honest
rather than ideal: a shared cache is Redis, and Redis is not wired into this
install. The consequences are stated rather than hidden — `describe()` says so,
and the hit rate is per replica. Correctness does not depend on it: a miss
recomputes, and a stale entry cannot outlive its generation.
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sutr.events import EventEnvelope, consumers, topics

logger = logging.getLogger(__name__)

CONSUMER = "discovery_cache"

# How long an entry may live even if nothing invalidates it. Short, because a
# trust score moves with deployments and calls that produce no registry event.
TTL_SECONDS = 300
# Entries per process. Bounded because an unbounded cache on a public search
# endpoint is a memory leak with a query string for a key.
MAX_ENTRIES = 2000

# Events that mean some tenant's answers may have changed. The LLD names tool
# publish and update; deprecation, archival and pricing are the same kind of
# fact and would otherwise leave a stale answer behind.
INVALIDATING = (
    topics.TOOL_PUBLISHED,
    topics.TOOL_UPDATED,
    topics.TOOL_DEPRECATED,
    topics.TOOL_ARCHIVED,
    topics.PRICING_UPDATED,
    topics.VERSION_CREATED,
    topics.SUBSCRIPTION_CREATED,
    topics.POLICY_UPDATED,
)

_lock = threading.Lock()
_entries: dict[str, tuple[float, int, Any]] = {}
_generations: dict[str, int] = {}
_global_generation = 0
_hits = 0
_misses = 0
_installed: list = []


@dataclass
class Stats:
    entries: int
    hits: int
    misses: int

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
        }


def generation(org_id: uuid.UUID) -> int:
    """This tenant's generation, plus the global one.

    A policy-version change is global — it changes what every tenant is allowed
    to see — so it bumps a counter every key includes.
    """
    with _lock:
        return _generations.get(str(org_id), 0) + _global_generation


def key(
    *,
    org_id: uuid.UUID,
    intent: str,
    policy_version: int,
    ranking_version: str,
    requirements: dict[str, Any],
    limit: int,
) -> str:
    """The LLD's key, plus what else actually changes the answer."""
    payload = json.dumps(
        {
            "tenant": str(org_id),
            "intent": " ".join(intent.lower().split()),
            "policy_version": policy_version,
            "ranking_version": ranking_version,
            "requirements": requirements,
            "limit": limit,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(cache_key: str, org_id: uuid.UUID) -> Any | None:
    global _hits, _misses
    current = generation(org_id)
    now = time.monotonic()
    with _lock:
        entry = _entries.get(cache_key)
        if entry is None:
            _misses += 1
            return None
        expires_at, minted_generation, value = entry
        if minted_generation != current or expires_at < now:
            # Expired, or minted before something changed. Dropped rather than
            # served with a warning: a stale ranking is a wrong answer.
            _entries.pop(cache_key, None)
            _misses += 1
            return None
        _hits += 1
        return value


def put(cache_key: str, org_id: uuid.UUID, value: Any) -> None:
    current = generation(org_id)
    with _lock:
        if len(_entries) >= MAX_ENTRIES:
            # Evict the entry closest to expiry. Not an LRU: this cache is
            # bounded to keep memory honest, not to maximise a hit rate.
            oldest = min(_entries, key=lambda existing: _entries[existing][0])
            _entries.pop(oldest, None)
        _entries[cache_key] = (time.monotonic() + TTL_SECONDS, current, value)


def invalidate(org_id: uuid.UUID) -> None:
    with _lock:
        _generations[str(org_id)] = _generations.get(str(org_id), 0) + 1


def invalidate_all() -> None:
    """For a policy change: every tenant's answers may now be different."""
    global _global_generation
    with _lock:
        _global_generation += 1


def clear() -> None:
    global _hits, _misses, _global_generation
    with _lock:
        _entries.clear()
        _generations.clear()
        _global_generation = 0
        _hits = 0
        _misses = 0


def stats() -> Stats:
    with _lock:
        return Stats(entries=len(_entries), hits=_hits, misses=_misses)


def _on_event(envelope: EventEnvelope) -> None:
    if envelope.event_type == topics.POLICY_UPDATED:
        invalidate_all()
        return
    if envelope.tenant_id:
        invalidate(uuid.UUID(envelope.tenant_id))
    # A tool published by one tenant changes what *every* tenant can discover,
    # because the storefront is cross-tenant. Only publication and withdrawal
    # have that reach; an edit to an already-listed tool does not.
    if envelope.event_type in (topics.TOOL_PUBLISHED, topics.TOOL_ARCHIVED):
        invalidate_all()


def install() -> None:
    if _installed:
        return
    for event_type in INVALIDATING:
        _installed.append(consumers.subscribe(CONSUMER, event_type, _on_event))


def uninstall() -> None:
    while _installed:
        consumers.unsubscribe(_installed.pop())


def describe() -> dict[str, Any]:
    return {
        "backend": "in-process",
        "key": "tenant + intent + policy version + ranking version + requirements + limit",
        "ttl_seconds": TTL_SECONDS,
        "max_entries": MAX_ENTRIES,
        "invalidated_by": list(INVALIDATING),
        "installed": bool(_installed),
        "stats": stats().as_dict(),
        "note": (
            "Per replica: each API process holds its own cache, because a shared one needs Redis "
            "and none is wired into this install. A miss recomputes, and an entry cannot outlive "
            "the generation it was minted in."
        ),
    }
