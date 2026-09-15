"""What is still per-process when this runs on more than one replica.

Scaling out is not free, and the parts that are not free are worth naming.
Most of this platform's state is in the database and behaves identically on one
replica or ten. A handful of things are deliberately in memory — a rate-limit
window, a cache, a set of parked long-polls — and each of them behaves
*differently* once there are several processes.

None of these is a correctness bug and none of them is hidden: this module is
the list, `/v1/platform/scaling` serves it, and a test asserts every entry
still names a module attribute that exists, so the list cannot quietly come to
describe a previous version of the code.

The work that genuinely must not run twice is not on this list — it runs under
a lease instead (`platform/leadership.py`).
"""

from dataclasses import dataclass

# How much a reader should care.
DEGRADED = "degraded"  # works, less well
DIVIDED = "divided"  # each replica holds its own share


@dataclass(frozen=True)
class ProcessLocalState:
    """One piece of state that lives in the process rather than the database."""

    name: str
    module: str
    attribute: str
    severity: str
    # What actually changes when there is more than one replica.
    effect: str
    # What to do about it, when there is something to do.
    remedy: str


PROCESS_LOCAL_STATE = (
    ProcessLocalState(
        name="authentication rate limits",
        module="sutr.rate_limit",
        attribute="_REGISTRY",
        severity=DEGRADED,
        effect=(
            "Each replica counts attempts it saw itself, so the effective limit is "
            "replicas × the configured one."
        ),
        remedy=(
            "Still a strong first brake against brute force. Where an exact limit matters, "
            "enforce it at the ingress, which sees every request."
        ),
    ),
    ProcessLocalState(
        name="tool-call rate limit",
        module="sutr.rate_limit",
        attribute="tool_call_limiter",
        severity=DEGRADED,
        effect=(
            "Per-organisation tool-call limits are counted per replica, so a busy tenant "
            "can reach replicas × TOOL_RATE_LIMIT_PER_MINUTE."
        ),
        remedy=(
            "Quotas — which are database-backed and exact — are the control for anything "
            "billable. This limiter is a brake on runaway loops, not an entitlement."
        ),
    ),
    ProcessLocalState(
        name="approval long-poll waiters",
        module="sutr.approvals.events",
        attribute="_events",
        severity=DEGRADED,
        effect=(
            "A caller parked on one replica is woken by a decision recorded on that same "
            "replica. A decision made through another replica does not wake it, and the "
            "long poll returns `timeout` instead."
        ),
        remedy=(
            "The decision itself is durable, so the caller sees it on its next poll. "
            "Postgres LISTEN/NOTIFY would close the gap; it is not wired."
        ),
    ),
    ProcessLocalState(
        name="discovery result cache",
        module="sutr.discovery.cache",
        attribute="_entries",
        severity=DIVIDED,
        effect=(
            "Each replica warms its own cache, so the hit rate is lower than a shared "
            "cache would give."
        ),
        remedy=(
            "Correctness is unaffected: invalidation rides the event bus, which every "
            "replica consumes, so no replica serves a result the registry has superseded."
        ),
    ),
    ProcessLocalState(
        name="circuit breaker state",
        module="sutr.resilience.breaker",
        attribute="_circuits",
        severity=DIVIDED,
        effect=(
            "Each replica decides for itself whether a provider is failing, so a provider "
            "that is down is discovered up to once per replica, and N replicas make up to "
            "N trial calls during a cool-off."
        ),
        remedy=(
            "Deliberate: a shared breaker would need a store on the hot path, and would let "
            "one replica's bad network stop every other replica's traffic to a provider "
            "that is fine. `/v1/resilience/providers` says which replica answered."
        ),
    ),
    ProcessLocalState(
        name="Prometheus registry",
        module="sutr.observability.metrics",
        attribute="REGISTRY",
        severity=DIVIDED,
        effect=(
            "Counters and histograms are per process. A scrape of one replica describes "
            "that replica only."
        ),
        remedy=(
            "Scrape every replica and aggregate — `sum(rate(...))` without a per-instance "
            "label, which is how the shipped dashboard is already written."
        ),
    ),
)


def describe() -> dict:
    """The inventory, for `/v1/platform/scaling`."""
    return {
        "process_local_state": [
            {
                "name": entry.name,
                "location": f"{entry.module}.{entry.attribute}",
                "severity": entry.severity,
                "effect": entry.effect,
                "remedy": entry.remedy,
            }
            for entry in PROCESS_LOCAL_STATE
        ],
        # Said plainly, because the list above is easy to read as a list of
        # bugs. It is not: it is the price of not putting a cache in the
        # request path's critical dependencies.
        "summary": (
            "Nothing on this list is a correctness bug. Work that must not run twice runs "
            "under a lease instead; see /v1/platform/leadership."
        ),
    }
