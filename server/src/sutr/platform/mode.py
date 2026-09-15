"""Whether this instance is the one that may write (LLD §5.4, §5.7).

The LLD's multi-region model is *"sync within region, async across regions"*:
one region owns the writes and the others follow it. With PostgreSQL streaming
replication, a follower's database physically refuses writes — every INSERT
comes back as *"cannot execute INSERT in a read-only transaction"*, which
reaches an agent as a 500 and tells it nothing.

A standby therefore has to know what it is. In `standby` mode this instance:

- serves reads normally, which is the point of keeping it warm;
- refuses writes with a named 503 and a `Retry-After`, rather than letting the
  driver produce a 500 the caller cannot act on;
- runs no singleton background loops, because all of them write.

This is a *foundation*, not a failover system. Nothing here promotes a standby,
routes traffic between regions, or measures replication lag: promotion is a
database operation and traffic is a DNS one, and pretending otherwise in
application code would be inventing a system that does not exist.
"""

from sutr.config import settings

ACTIVE = "active"
STANDBY = "standby"
MODES = (ACTIVE, STANDBY)

# Methods that change state. A standby refuses these unless the route is
# declared below.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# POST routes that read. They exist because a query too long or too structured
# for a URL is still a query: discovery takes an intent and a requirements
# object, and the policy decision point takes the request it is being asked
# about. Neither writes a row, so neither has any reason to fail on a standby.
#
# Every path here is literal — no path parameters — so matching is an exact
# comparison rather than a router walk on the hot path. A test asserts each one
# is a real route, so the list cannot quietly come to name something that moved.
READ_ONLY_WRITES = frozenset(
    {
        "/v1/discovery/search",
        "/v1/discovery/evaluate",
        "/v1/governance/evaluate",
    }
)

# Paths that must answer on a standby whatever their method, because they are
# how an operator or an orchestrator finds out that it *is* a standby.
ALWAYS_ALLOWED = frozenset({"/health", "/health/live", "/health/ready", "/metrics"})


def current() -> str:
    """The configured mode, defaulting to active for anything unrecognised.

    Defaulting to active is deliberate: a typo in an environment variable
    should not silently turn a region that is serving writes into one that
    refuses them.
    """
    configured = (settings.platform_mode or ACTIVE).strip().lower()
    return configured if configured in MODES else ACTIVE


def is_standby() -> bool:
    return current() == STANDBY


def writes_allowed() -> bool:
    return not is_standby()


def allows(method: str, path: str) -> bool:
    """Whether this instance will serve `method path` in its current mode."""
    if not is_standby():
        return True
    if path in ALWAYS_ALLOWED or path in READ_ONLY_WRITES:
        return True
    return method.upper() not in WRITE_METHODS


def refusal_message() -> str:
    primary = settings.primary_region or "the active region"
    return (
        f"This instance is a read-only standby ({settings.region or 'region unset'}). "
        f"Writes are served by {primary}."
    )


def describe() -> dict:
    """What this instance is, for the readiness and capability reports."""
    return {
        "mode": current(),
        "writes_allowed": writes_allowed(),
        "region": settings.region or None,
        # Where writes go. Recorded rather than discovered: this platform does
        # not elect a primary, and reporting a guess would be worse than
        # reporting nothing.
        "primary_region": settings.primary_region or None,
        "read_only_writes": sorted(READ_ONLY_WRITES),
    }
