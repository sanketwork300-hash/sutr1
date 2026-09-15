"""What this instance is, and what it can actually do right now.

ADR-015 says every optional backend degrades rather than failing, and that
*"feature availability becomes a runtime property that must be reported
honestly"*. This is that report. An operator asking "is Kafka on?" or "why is
discovery lexical-only?" should get an answer from the platform rather than
from someone's memory of the deployment.
"""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from sutr.common import envelope
from sutr.config import settings
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.events.bus import get_bus
from sutr.platform import leadership, mode, scaling
from sutr.platform.boundaries import ALL_SERVICES, Plane, services_in

router = APIRouter(prefix="/v1/platform", tags=["platform"])


def _capability(name: str, available: bool, reason: str | None, detail: str) -> dict:
    return {
        "name": name,
        "available": available,
        # Present only when unavailable: a reason attached to a working
        # capability reads as a warning about something that is fine.
        "reason": None if available else reason,
        "detail": detail,
    }


def capabilities() -> list[dict]:
    """Which backends are live, and what is degraded without them."""
    bus = get_bus()
    bus_ok, bus_reason = bus.available()

    entries = [
        _capability(
            "event_bus",
            bus_ok,
            bus_reason,
            f"Backend: {bus.name}. Events are always written to the outbox first, so a bus "
            "that is down delays delivery rather than losing facts.",
        ),
        _capability(
            "secrets_kms",
            settings.secrets_backend == "db_kms",
            "SECRETS_BACKEND is 'db', so secret values are stored unencrypted in the database.",
            "Envelope encryption for stored secrets.",
        ),
        _capability(
            "tracing",
            settings.otel_enabled,
            "OTEL_ENABLED is false.",
            "OpenTelemetry spans for tool execution.",
        ),
        _capability(
            "metrics",
            settings.metrics_enabled,
            "METRICS_ENABLED is false, so /metrics returns 404.",
            "Prometheus metrics endpoint.",
        ),
        _capability(
            "mcp_sse",
            settings.mcp_sse_enabled,
            "MCP_SSE_ENABLED is false.",
            "MCP over SSE at /sse and /messages, for clients that do not speak Streamable HTTP.",
        ),
        _capability(
            "billing",
            settings.billing_enabled(),
            "Stripe is not configured, or this is a self-hosted instance.",
            "Subscription billing.",
        ),
        _capability(
            "writes",
            mode.writes_allowed(),
            f"PLATFORM_MODE is '{mode.current()}': this instance serves reads only.",
            "Accepting requests that change state. A standby refuses them with a 503 that "
            "names where writes are served.",
        ),
        _capability(
            "usage_events",
            settings.emit_usage_events,
            "EMIT_USAGE_EVENTS is false: the usage ledger is authoritative and nothing "
            "consumes the event yet.",
            "A usage.recorded event per invocation, for downstream billing consumers.",
        ),
    ]
    return entries


@router.get("/capabilities")
def get_capabilities(_: AgentAuth = Depends(get_agent_auth)) -> dict:
    entries = capabilities()
    return envelope(
        {
            "capabilities": entries,
            "degraded": [entry["name"] for entry in entries if not entry["available"]],
        }
    )


@router.get("/services")
def get_services(_: AgentAuth = Depends(get_agent_auth)) -> dict:
    """The service map and plane split this build was assembled from.

    Useful when reading logs or planning a split: it answers "which service
    owns this table?" without grepping.
    """
    return envelope(
        {
            "planes": {
                plane.value: [
                    {
                        "name": service.name,
                        "description": service.description,
                        "tables": list(service.tables),
                    }
                    for service in services_in(plane)
                ]
                for plane in Plane
            },
            "service_count": len(ALL_SERVICES),
        }
    )


@router.get("/mode")
def get_mode(_: AgentAuth = Depends(get_agent_auth)) -> dict:
    """Whether this instance may write, and which region owns writes if not."""
    return envelope(mode.describe())


@router.get("/leadership")
def get_leadership(
    _: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """Which replica is running each job that must run exactly once.

    Answers "which one is sweeping?" — the question an operator asks first when
    a background job has not run, and the one that is hardest to answer from
    outside.
    """
    described = leadership.describe()
    described["holders"] = {
        job: leadership.holder(session, job) for job in leadership.SINGLETON_JOBS
    }
    return envelope(described)


@router.get("/scaling")
def get_scaling(_: AgentAuth = Depends(get_agent_auth)) -> dict:
    """What is still per-process when this runs on more than one replica."""
    return envelope(scaling.describe())
