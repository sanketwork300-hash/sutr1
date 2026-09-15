"""What one tenant is allowed to see of its own telemetry (LLD §5.3).

The observability stack this platform ships is shared: one Prometheus, one
Loki, one Tempo, holding every tenant's data. None of it is safe to expose to a
tenant, and `/metrics` deliberately carries no tenant labels at all — see
`metrics.py` for that decision.

Tenant-isolated telemetry is therefore served from *this* platform's own
tables, where every row already has an owner. The read model here answers the
three questions a tenant actually asks — how much did I call, how did it go,
and what happened during that one operation — from `log_entry`, scoped to the
caller's organisation on every query with no way to widen the scope.

What it does not do: query Prometheus, Loki or Tempo on a tenant's behalf. That
would need per-tenant tenancy in those backends and a query proxy that enforces
it; neither exists here, and `describe()` says so rather than implying that a
missing feature is a configuration problem.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import Session, select

from sutr.models.log import LogEntry

# The most rows one summary will read. A tenant with a busy month would
# otherwise turn a dashboard refresh into a table scan; the response says when
# it has been truncated rather than quietly describing a fraction of the period
# as though it were the whole of it.
MAX_SAMPLE = 5000

DEFAULT_WINDOW_HOURS = 24


def window(hours: int | None = None, *, maximum_hours: int) -> tuple[datetime, datetime]:
    """The [start, end) the caller asked for, clamped to what is allowed."""
    requested = DEFAULT_WINDOW_HOURS if hours is None else max(1, hours)
    end = datetime.utcnow()
    return end - timedelta(hours=min(requested, maximum_hours)), end


def summary(session: Session, *, org_id: uuid.UUID, start: datetime, end: datetime) -> dict:
    """Rate, errors and latency for one tenant's own calls."""
    rows = session.exec(
        select(LogEntry.outcome, LogEntry.duration_ms, LogEntry.integration_id)
        .where(LogEntry.org_id == org_id)
        .where(LogEntry.timestamp >= start)
        .where(LogEntry.timestamp < end)
        .order_by(LogEntry.timestamp.desc())
        .limit(MAX_SAMPLE)
    ).all()

    outcomes: dict[str, int] = {}
    durations: list[int] = []
    per_provider: dict[str, list[int]] = {}
    for outcome, duration_ms, integration_id in rows:
        outcomes[outcome or "unknown"] = outcomes.get(outcome or "unknown", 0) + 1
        if duration_ms is None:
            continue
        durations.append(duration_ms)
        per_provider.setdefault(integration_id, []).append(duration_ms)

    errors = outcomes.get("error", 0) + outcomes.get("denied", 0)
    total = len(rows)
    seconds = max((end - start).total_seconds(), 1.0)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "calls": total,
        "truncated": total >= MAX_SAMPLE,
        "by_outcome": outcomes,
        "errors": errors,
        # Kept as a ratio rather than a percentage so a caller does not have to
        # guess which one it is looking at.
        "error_ratio": round(errors / total, 4) if total else 0.0,
        "calls_per_minute": round(total / (seconds / 60), 3),
        "latency_ms": _percentiles(durations),
        "by_provider": {
            provider: {"calls": len(values), "latency_ms": _percentiles(values)}
            for provider, values in sorted(per_provider.items())
        },
    }


def operations(
    session: Session, *, org_id: uuid.UUID, start: datetime, end: datetime, limit: int = 50
) -> list[dict]:
    """One row per correlation id: the tenant's recent operations, newest first.

    An operation is whatever shared a correlation id — usually one request, but
    an approval that was granted and then executed is two requests and one
    operation, which is exactly why the id exists.
    """
    rows = session.exec(
        select(
            LogEntry.correlation_id,
            func.count(LogEntry.id),
            func.min(LogEntry.timestamp),
            func.max(LogEntry.timestamp),
            func.sum(LogEntry.duration_ms),
            func.max(LogEntry.trace_id),
        )
        .where(LogEntry.org_id == org_id)
        .where(LogEntry.correlation_id.is_not(None))
        .where(LogEntry.timestamp >= start)
        .where(LogEntry.timestamp < end)
        .group_by(LogEntry.correlation_id)
        .order_by(func.max(LogEntry.timestamp).desc())
        .limit(limit)
    ).all()

    return [
        {
            "correlation_id": correlation_id,
            "steps": steps,
            "started_at": first.isoformat() if first else None,
            "ended_at": last.isoformat() if last else None,
            "duration_ms": int(total_ms) if total_ms is not None else None,
            "trace_id": trace_id,
        }
        for correlation_id, steps, first, last, total_ms, trace_id in rows
    ]


def operation(session: Session, *, org_id: uuid.UUID, correlation_id: str) -> dict | None:
    """The timeline of one operation, or None if this tenant has no such id.

    None rather than an empty timeline: an id that belongs to another tenant
    and an id that never existed must be indistinguishable from here, or the
    endpoint becomes a way to ask whether another tenant made a given call.
    """
    rows = session.exec(
        select(LogEntry)
        .where(LogEntry.org_id == org_id)
        .where(LogEntry.correlation_id == correlation_id)
        .order_by(LogEntry.timestamp)
    ).all()
    if not rows:
        return None

    trace_ids = {row.trace_id for row in rows if row.trace_id}
    return {
        "correlation_id": correlation_id,
        # The id an operator pastes into the trace backend. Absent when tracing
        # was off for these calls, which is the default install.
        "trace_id": next(iter(trace_ids), None),
        "steps": [
            {
                "timestamp": row.timestamp.isoformat(),
                "provider_id": row.integration_id,
                "tool_id": row.tool_name,
                "outcome": row.outcome,
                "duration_ms": row.duration_ms,
                "access_reason": row.access_reason,
                # The error text, already redacted when it was stored.
                "error": row.error,
            }
            for row in rows
        ],
    }


def describe() -> dict:
    """What a tenant can and cannot get here.

    Stated in the payload because the alternative is an operator inferring it
    from an empty response.
    """
    return {
        "source": "this platform's own call log",
        "scope": "the calling organisation only",
        "max_sample": MAX_SAMPLE,
        "metrics_backend_queries": False,
        "log_backend_queries": False,
        "trace_backend_queries": False,
        "detail": (
            "Prometheus, Loki and Tempo hold every tenant's telemetry and are not "
            "queried on a tenant's behalf: doing that safely needs per-tenant tenancy "
            "in those backends and a query proxy that enforces it, and neither is "
            "implemented here. The trace id on an operation is what joins this "
            "record to a trace an operator can look up."
        ),
    }


def _percentiles(values: list[int]) -> dict:
    """p50/p95/p99 by nearest rank — no interpolation, so every number returned
    is a latency that was actually observed."""
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def at(fraction: float) -> int:
        rank = max(1, min(len(ordered), int(-(-len(ordered) * fraction // 1))))
        return ordered[rank - 1]

    return {
        "count": len(ordered),
        "p50": at(0.50),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": ordered[-1],
    }
