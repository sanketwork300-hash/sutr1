"""Tenant-isolated telemetry (ESDS LLD §5.3).

§5.3 asks for *tenant-isolated telemetry* alongside the shared stack. The
shared stack is not the place to serve it from: one Prometheus and one Tempo
hold every tenant's data, and `/metrics` carries no tenant labels by design.

So a tenant reads its own telemetry here — its call rate, its error ratio, its
latency percentiles, and the timeline of one operation — from this platform's
own tables, scoped to the caller's organisation by the query itself rather than
by a filter a future edit could drop.

The LLD names no paths for this surface, so these are ours: `/v1/observability`
alongside the rest of the `/v1` API (ADR-004).
"""

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import NotFoundError
from sutr.config import settings
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.observability import tenant
from sutr.observability.log_context import LOG_FIELDS
from sutr.observability.tracing import describe as describe_tracing

router = APIRouter(prefix="/v1/observability", tags=["observability"])

# Telemetry about a tenant's own calls is the same class of information as the
# call log itself, and is read under the same permission.
READ = "logs:read"


@router.get("/capabilities")
def capabilities(
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """What this install's telemetry actually does right now."""
    ensure_agent_can(session, auth, READ)
    return envelope(
        {
            "logging": {
                "format": settings.log_format,
                "level": settings.log_level,
                # Redaction happens in the formatter, so it holds for every
                # line regardless of what the call site passed.
                "redaction": "formatter",
                "fields": list(LOG_FIELDS),
            },
            "metrics": {
                "endpoint": "/metrics" if settings.metrics_enabled else None,
                "enabled": settings.metrics_enabled,
                "authenticated": bool(settings.metrics_token),
                "tenant_labels": False,
            },
            "tracing": describe_tracing(),
            "tenant_telemetry": tenant.describe(),
        }
    )


@router.get("/summary")
def summary(
    window_hours: int = Query(default=tenant.DEFAULT_WINDOW_HOURS, ge=1),
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """Call rate, error ratio and latency percentiles for the caller's org."""
    ensure_agent_can(session, auth, READ)
    start, end = tenant.window(window_hours, maximum_hours=settings.telemetry_window_hours_max)
    return envelope(tenant.summary(session, org_id=auth.org.id, start=start, end=end))


@router.get("/operations")
def list_operations(
    window_hours: int = Query(default=tenant.DEFAULT_WINDOW_HOURS, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """The caller's recent operations, one row per correlation id."""
    ensure_agent_can(session, auth, READ)
    start, end = tenant.window(window_hours, maximum_hours=settings.telemetry_window_hours_max)
    found = tenant.operations(session, org_id=auth.org.id, start=start, end=end, limit=limit)
    return envelope({"operations": found, "count": len(found)})


@router.get("/operations/{correlation_id}")
def get_operation(
    correlation_id: str,
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """One operation's timeline.

    An id belonging to another tenant is a 404 here, exactly as an id that
    never existed is: any other answer would turn this route into a way to ask
    whether another tenant made a particular call.
    """
    ensure_agent_can(session, auth, READ)
    found = tenant.operation(session, org_id=auth.org.id, correlation_id=correlation_id)
    if found is None:
        raise NotFoundError(f"No operation {correlation_id} was found for this organisation.")
    return envelope(found)
