"""Liveness and readiness are different questions (LLD §5.8 self-healing).

Answering both with one endpoint gets one of them wrong. A liveness probe that
fails when the database is down restarts every replica at once and turns a
recoverable database incident into a longer one; a readiness probe that ignores
the database keeps sending traffic to a replica that cannot serve it.
"""

import pytest

from sutr.config import settings
from sutr.platform import leadership


async def test_the_original_health_endpoint_is_unchanged(client):
    """The compose healthcheck, fly.toml and existing deployments point here."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_liveness_depends_on_nothing_but_the_process(client, monkeypatch):
    """Deliberately: a liveness probe that failed on a database outage would
    restart every replica at once."""

    def explode(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr("sutr.db.engine.connect", explode)

    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["instance_id"] == leadership.instance_id()


async def test_readiness_checks_the_database(client):
    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] is True
    assert "pool" in body["checks"]["database"]


async def test_a_replica_that_cannot_reach_the_database_leaves_the_rotation(client, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("connection refused to db.internal:5432 as user sutr")

    monkeypatch.setattr("sutr.db.engine.connect", explode)

    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False
    # The probe reader is an orchestrator. Hosts, users and ports belong in the
    # log, not in an unauthenticated response.
    assert "db.internal" not in response.text
    assert "sutr" not in body["checks"]["database"]["detail"]


async def test_readiness_says_which_replica_answered_and_what_it_is_running(client, session):
    from sutr.platform.leadership import acquire

    acquire(session, leadership.JOB_MAINTENANCE)

    body = (await client.get("/health/ready")).json()

    assert body["instance_id"] == leadership.instance_id()
    assert leadership.JOB_MAINTENANCE in body["leadership"]


async def test_readiness_names_the_region(client, monkeypatch):
    monkeypatch.setattr(settings, "region", "ap-south-1")
    assert (await client.get("/health/ready")).json()["region"] == "ap-south-1"


async def test_an_unset_region_is_null_rather_than_a_guess(client, monkeypatch):
    monkeypatch.setattr(settings, "region", "")
    assert (await client.get("/health/ready")).json()["region"] is None


async def test_the_probes_are_not_in_the_public_schema(client):
    """They are infrastructure, not API. An SDK has no business calling them."""
    schema = (await client.get("/openapi.json")).json()
    for path in ("/health", "/health/live", "/health/ready"):
        assert path not in schema["paths"]


@pytest.mark.parametrize("path", ["/health", "/health/live", "/health/ready"])
async def test_the_probes_need_no_credentials(unauthenticated_client, path):
    """A probe that needs a token is a probe that will be configured wrong."""
    assert (await unauthenticated_client.get(path)).status_code in (200, 503)


def test_the_pool_report_says_which_dialect_it_describes():
    from sutr import db

    status = db.pool_status()
    assert "dialect" in status
