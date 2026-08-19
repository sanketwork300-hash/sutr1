"""Usage endpoints: org-scoped reads over the metering ledger.

Aggregation happens in SQL (grouped counts/sums), not by pulling rows into
Python, so a busy org's month doesn't have to fit in memory.

Auth uses `get_agent_auth` rather than a human-only JWT dependency so API keys
can read their own org's usage — the CLI and SDKs authenticate that way, and
reading aggregate counts is strictly less privileged than the tool execution
those keys already perform. Human callers are still role-checked (`logs:read`).
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, col, select

from sutr.authz import ensure_agent_can
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.models.usage_event import KIND_TOOL_CALL, UsageEvent

router = APIRouter(prefix="/api/usage", tags=["usage"])

MAX_RANGE_DAYS = 366


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalise an inbound timestamp to naive UTC.

    Clients send offset-aware ISO strings (JS `toISOString()` ends in "Z", so
    both the UI and the CLI do), while the ledger's columns — like every
    datetime in this codebase — are naive UTC. Comparing the two raises
    TypeError, so aware input is converted rather than rejected.
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _resolve_range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    start, end = _as_naive_utc(start), _as_naive_utc(end)
    now = datetime.utcnow()
    end_at = end or now
    start_at = start or (end_at - timedelta(days=30))
    if start_at > end_at:
        raise HTTPException(status_code=400, detail="start must be before end")
    if (end_at - start_at) > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(status_code=400, detail=f"Range must not exceed {MAX_RANGE_DAYS} days")
    return start_at, end_at


def _scoped(org_id: uuid.UUID, start_at: datetime, end_at: datetime):
    return (
        (UsageEvent.org_id == org_id),
        (col(UsageEvent.timestamp) >= start_at),
        (col(UsageEvent.timestamp) <= end_at),
    )


@router.get("/summary")
def usage_summary(
    start: datetime | None = None,
    end: datetime | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Totals and breakdowns for the window (default: last 30 days)."""
    ensure_agent_can(session, agent_auth, "logs:read")
    start_at, end_at = _resolve_range(start, end)
    where = _scoped(agent_auth.org.id, start_at, end_at)

    totals = session.exec(
        select(
            UsageEvent.kind,
            func.sum(col(UsageEvent.quantity)),
            func.count(),
        )
        .where(*where)
        .group_by(col(UsageEvent.kind))
    ).all()

    tool_where = (*where, UsageEvent.kind == KIND_TOOL_CALL)

    by_outcome = session.exec(
        select(UsageEvent.outcome, func.count())
        .where(*tool_where)
        .group_by(col(UsageEvent.outcome))
    ).all()

    by_source = session.exec(
        select(UsageEvent.source, func.count()).where(*tool_where).group_by(col(UsageEvent.source))
    ).all()

    by_integration = session.exec(
        select(UsageEvent.integration_id, func.count())
        .where(*tool_where)
        .group_by(col(UsageEvent.integration_id))
        .order_by(func.count().desc())
        .limit(20)
    ).all()

    by_tool = session.exec(
        select(UsageEvent.integration_id, UsageEvent.tool_name, func.count())
        .where(*tool_where)
        .group_by(col(UsageEvent.integration_id), col(UsageEvent.tool_name))
        .order_by(func.count().desc())
        .limit(20)
    ).all()

    # Daily series. func.date() works on both SQLite and Postgres for a
    # DateTime column and yields YYYY-MM-DD.
    day = func.date(col(UsageEvent.timestamp))
    daily = session.exec(
        select(day, func.count()).where(*tool_where).group_by(day).order_by(day)
    ).all()

    durations = session.exec(
        select(
            func.avg(col(UsageEvent.duration_ms)),
            func.max(col(UsageEvent.duration_ms)),
        ).where(*tool_where, col(UsageEvent.duration_ms).is_not(None))
    ).first()

    tool_calls = next((int(qty or 0) for kind, qty, _ in totals if kind == KIND_TOOL_CALL), 0)
    return {
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "tool_calls": tool_calls,
        "totals_by_kind": [
            {"kind": kind, "quantity": int(qty or 0), "events": int(events or 0)}
            for kind, qty, events in totals
        ],
        "tool_calls_by_outcome": [
            {"outcome": outcome, "count": int(count)} for outcome, count in by_outcome
        ],
        "tool_calls_by_source": [
            {"source": source, "count": int(count)} for source, count in by_source
        ],
        "top_integrations": [
            {"integration_id": integration_id, "count": int(count)}
            for integration_id, count in by_integration
        ],
        "top_tools": [
            {"integration_id": integration_id, "tool_name": tool_name, "count": int(count)}
            for integration_id, tool_name, count in by_tool
        ],
        "daily": [{"date": str(date), "count": int(count)} for date, count in daily],
        "duration_ms": {
            "avg": round(float(durations[0]), 1)
            if durations and durations[0] is not None
            else None,
            "max": int(durations[1]) if durations and durations[1] is not None else None,
        },
    }


@router.get("/events")
def usage_events(
    start: datetime | None = None,
    end: datetime | None = None,
    kind: str | None = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    """Raw metered events — for export and reconciliation."""
    ensure_agent_can(session, agent_auth, "logs:read")
    start_at, end_at = _resolve_range(start, end)
    stmt = (
        select(UsageEvent)
        .where(*_scoped(agent_auth.org.id, start_at, end_at))
        .order_by(col(UsageEvent.id).desc())
    )
    if kind:
        stmt = stmt.where(UsageEvent.kind == kind)
    rows = session.exec(stmt.offset(offset).limit(limit)).all()
    return [e.model_dump() for e in rows]
