"""Active and standby (ESDS LLD §5.4, §5.7).

A follower region's database physically refuses writes, so every INSERT comes
back as *"cannot execute INSERT in a read-only transaction"* — a 500 that tells
an agent nothing and that it will retry against the same instance. Standby mode
turns that into a 503 that names the mode, names where writes are served, and
carries `Retry-After`.

The interesting cases are the edges: a POST that only reads must still work, a
probe must still answer, and an unrecognised mode must not accidentally take a
region out of service.
"""

import pytest

from sutr.config import settings
from sutr.platform import mode


@pytest.fixture(name="standby")
def standby_fixture(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "standby")
    monkeypatch.setattr(settings, "region", "ap-south-2")
    monkeypatch.setattr(settings, "primary_region", "ap-south-1")


# ── Which mode is this? ──────────────────────────────────────────────────────


def test_active_is_the_default():
    assert mode.current() == mode.ACTIVE
    assert mode.writes_allowed() is True


def test_a_typo_means_active_rather_than_a_region_that_refuses_everything(monkeypatch):
    """A misspelled environment variable must not quietly stop serving writes."""
    monkeypatch.setattr(settings, "platform_mode", "stanby")
    assert mode.current() == mode.ACTIVE


def test_the_mode_is_read_at_call_time_not_at_import(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "standby")
    assert mode.is_standby() is True
    monkeypatch.setattr(settings, "platform_mode", "active")
    assert mode.is_standby() is False


def test_case_and_whitespace_do_not_decide_a_regions_role(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "  STANDBY ")
    assert mode.current() == mode.STANDBY


# ── What a standby serves ────────────────────────────────────────────────────


async def test_a_standby_refuses_a_write_with_a_reason(client, standby):
    response = await client.post("/v1/registry/tools", json={})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    body = response.json()
    assert body["error"]["code"] == "unavailable"
    # An agent reading this learns where to go, not just that something failed.
    assert "ap-south-1" in body["error"]["message"]
    assert body["error"]["details"]["mode"] == "standby"


async def test_a_standby_serves_reads(client, standby):
    assert (await client.get("/v1/platform/capabilities")).status_code == 200


async def test_a_standby_answers_its_probes(client, standby):
    """An orchestrator finds out that this is a standby by asking it."""
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/health/live")).status_code == 200

    ready = await client.get("/health/ready")
    assert ready.status_code == 200  # serving reads is what it is for
    assert ready.json()["mode"] == "standby"
    assert ready.json()["writes_allowed"] is False


async def test_a_standby_still_answers_a_post_that_only_reads(client, standby):
    """Discovery takes an intent and a requirements object — too structured for
    a URL, and it writes nothing."""
    response = await client.post("/v1/discovery/search", json={"intent": "refund a payment"})
    assert response.status_code != 503


async def test_the_read_only_allow_list_names_routes_that_exist():
    """Otherwise the list quietly comes to describe a previous version."""
    from sutr.main import app
    from tests.conftest import served_paths

    paths = served_paths(app)
    for allowed in mode.READ_ONLY_WRITES:
        assert allowed in paths, f"{allowed} is allow-listed but is not a route"


async def test_the_read_only_allow_list_names_routes_that_do_not_write():
    """Structural: an allow-listed handler that commits would be a write served
    by a database that refuses writes."""
    import inspect

    from sutr.api.v1 import discovery, governance

    for module, names in ((discovery, ("search", "evaluate")), (governance, ("evaluate",))):
        for name in names:
            source = inspect.getsource(getattr(module, name))
            assert "session.commit()" not in source, f"{module.__name__}.{name} commits"


async def test_every_delete_and_patch_is_refused_too(client, standby):
    for call in (
        client.delete("/v1/registry/tools/00000000-0000-0000-0000-000000000000"),
        client.patch("/api/orgs/settings", json={}),
    ):
        assert (await call).status_code == 503


async def test_an_active_instance_refuses_nothing(client):
    """The default path: one string comparison, and then business as usual."""
    response = await client.post("/v1/registry/tools", json={})
    assert response.status_code != 503


# ── What it reports about itself ─────────────────────────────────────────────


async def test_the_platform_report_says_writes_are_off(client, standby):
    body = (await client.get("/v1/platform/capabilities")).json()["data"]

    writes = next(entry for entry in body["capabilities"] if entry["name"] == "writes")
    assert writes["available"] is False
    assert "standby" in writes["reason"]
    assert "writes" in body["degraded"]


async def test_the_mode_endpoint_names_the_region_that_owns_writes(client, standby):
    body = (await client.get("/v1/platform/mode")).json()["data"]

    assert body["mode"] == "standby"
    assert body["region"] == "ap-south-2"
    assert body["primary_region"] == "ap-south-1"
    assert sorted(body["read_only_writes"]) == sorted(mode.READ_ONLY_WRITES)


async def test_an_unset_primary_region_is_null_rather_than_a_guess(client, monkeypatch):
    """This platform does not elect a primary. Reporting one would be invention."""
    monkeypatch.setattr(settings, "primary_region", "")
    body = (await client.get("/v1/platform/mode")).json()["data"]
    assert body["primary_region"] is None


def test_the_refusal_names_the_active_region_even_when_none_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "platform_mode", "standby")
    monkeypatch.setattr(settings, "primary_region", "")
    assert "the active region" in mode.refusal_message()
