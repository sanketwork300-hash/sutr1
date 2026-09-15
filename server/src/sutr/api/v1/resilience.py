"""What this platform does when something fails (ESDS LLD §5.8).

§5.8 names five failure-handling patterns, eight failure domains and four
severities. This is where an operator reads which of them this build actually
implements, and — for the circuit breaker — what it currently thinks of each
provider.

The LLD names no paths for this, so these are ours (ADR-004).
"""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.resilience import breaker, domains, retry, timeouts
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/resilience", tags=["resilience"])

READ = "logs:read"
# Closing a circuit by hand is an operational act on shared state: every tenant
# on this replica is affected by it.
MANAGE = "org:manage"


@router.get("/policy")
def get_policy(
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """The failure-handling policy this build was assembled with."""
    ensure_agent_can(session, auth, READ)
    return envelope(
        {
            "timeouts": timeouts.describe(),
            "retries": retry.describe(),
            "circuit_breaker": breaker.describe(),
            **domains.describe(),
            # Named rather than implied: two of the five §5.8 patterns are not
            # here, and an operator reading a policy document should not have
            # to infer that from silence.
            "not_implemented": {
                "bulkheads": (
                    "There are no isolated pools per workload. Every request shares one "
                    "process and one database pool, so a slow workload can starve a fast "
                    "one. The circuit breaker limits how long any single provider can hold "
                    "a worker, which is a partial substitute and not the pattern."
                ),
            },
        }
    )


@router.get("/providers")
def get_providers(
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """Circuit state and health score per provider, as *this replica* sees it.

    Per replica, because the breaker is per process (see
    `/v1/platform/scaling`). Another replica may be serving a provider this one
    has quarantined, which is deliberate: one replica's bad network should not
    take a provider away from every other.
    """
    ensure_agent_can(session, auth, READ)
    observed = breaker.snapshot()
    return envelope(
        {
            "providers": observed,
            "quarantined": [entry["provider"] for entry in observed if entry["quarantined"]],
            "scope": "this replica only",
        }
    )


@router.post("/providers/{provider}/close", status_code=200)
def close_circuit(
    provider: str,
    auth: AgentAuth = Depends(get_agent_auth),
    session: Session = Depends(get_session),
) -> dict:
    """Close a circuit now, for an operator who has fixed the upstream.

    Audited, because it is a deliberate act on shared state that changes what
    every caller on this replica experiences.
    """
    ensure_agent_can(session, auth, MANAGE)
    if breaker.health(provider)["state"] == breaker.CLOSED:
        raise NotFoundError(f"No open circuit for {provider} on this replica.")

    breaker.reset(provider)
    record_audit(
        session,
        org_id=auth.org.id,
        action="resilience.circuit_closed",
        summary=f"Circuit for {provider} closed by hand on this replica",
        target_type="provider",
        target_id=provider,
        **actor_from_agent_auth(auth),
    )
    session.commit()
    return envelope({"provider": provider, **breaker.health(provider)})
