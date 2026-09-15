"""Liveness and readiness, which are different questions.

An orchestrator asks two things, and answering both with one endpoint gets one
of them wrong:

- **Liveness**: is this process wedged and in need of a restart? It must not
  depend on anything else. A liveness probe that fails when the database is
  down turns a database outage into a restart loop across every replica, which
  is how a recoverable incident becomes a longer one.
- **Readiness**: should this replica receive traffic *right now*? It checks the
  dependencies a request needs, so a replica that cannot reach the database is
  taken out of rotation while it stays alive and keeps its metrics.

`GET /health` is kept exactly as it was — the compose healthcheck, the Fly
config and existing deployments all point at it — and is an alias for liveness.
"""

import logging

from fastapi import APIRouter, Response
from sqlalchemy import text

from sutr import db
from sutr.config import settings
from sutr.platform import leadership, mode

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", include_in_schema=False)
def health() -> dict:
    """The original endpoint. Liveness, under its original name and shape."""
    return {"status": "ok"}


@router.get("/health/live", include_in_schema=False)
def live() -> dict:
    """Answers if the event loop is turning. Deliberately checks nothing else."""
    return {"status": "ok", "instance_id": leadership.instance_id()}


@router.get("/health/ready", include_in_schema=False)
def ready(response: Response) -> dict:
    """Whether this replica should be given traffic.

    503 when the database is unreachable, because every meaningful request
    needs it. A standby is **ready**: serving reads is what it is for, and its
    refusal of writes is a named 503 per request, not a reason to take the
    whole replica out of rotation.
    """
    database = _database_check()
    ok = database["ok"]
    if not ok:
        response.status_code = 503

    return {
        "status": "ready" if ok else "not_ready",
        "instance_id": leadership.instance_id(),
        "region": settings.region or None,
        "mode": mode.current(),
        "writes_allowed": mode.writes_allowed(),
        "checks": {"database": database},
        # Which singleton jobs this replica currently owns. An operator asking
        # "which one is sweeping?" gets the answer from a probe rather than
        # from the database.
        "leadership": leadership.held_jobs(),
    }


def _database_check() -> dict:
    """One trivial query. Never raises — an unreachable database is an answer."""
    try:
        with db.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("readiness: database unreachable: %s", exc)
        # The exception text can name hosts, users and ports. The probe reader
        # is an orchestrator; the detail belongs in the log, which is where it
        # just went.
        return {"ok": False, "detail": "unreachable"}
    return {"ok": True, "pool": db.pool_status()}
