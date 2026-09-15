"""The breaker where it matters: in front of a real tool call (LLD §5.8).

The unit tests pin the state machine. These pin the thing an operator actually
cares about — that a provider which is down stops costing a worker per call,
that an agent gets a fast named refusal instead of a slow timeout, and that the
platform's own errors are never blamed on the provider.
"""

import json

import httpx
import pytest

from sutr.config import settings
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration import InstalledIntegration
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.resilience import breaker
from sutr.services.tool_pipeline import CallContext, evaluate_gate, execute_tool

HOST = "api.provider.example"


@pytest.fixture(autouse=True)
def clean_circuits(monkeypatch):
    breaker.reset()
    monkeypatch.setattr(settings, "circuit_breaker_enabled", True)
    monkeypatch.setattr(settings, "circuit_breaker_failure_threshold", 3)
    monkeypatch.setattr(settings, "provider_retry_attempts", 1)
    yield
    breaker.reset()


@pytest.fixture(name="provider")
def provider_fixture(session, test_org):
    tool = {
        "name": "fetch_thing",
        "description": "Fetch a thing",
        "method": "GET",
        "path": "/things",
        "params": [],
    }
    session.add(
        CustomApiIntegration(
            org_id=test_org.id,
            integration_id="customapi_provider",
            name="Provider",
            base_url=f"https://{HOST}",
            tools_json=json.dumps([tool]),
        )
    )
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="customapi_provider",
            type="custom",
            url=f"https://{HOST}",
            auth_method="none",
            connected=True,
        )
    )
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id,
            integration_id="customapi_provider",
            tool_name="fetch_thing",
            mode="allow",
        )
    )
    session.commit()


# The real class, captured before any test replaces it. Subclassing
# `httpx.AsyncClient` at patch time would subclass a *previous* test's stub and
# inherit its transport, which is a confusing way to spend an afternoon.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _answer_with(monkeypatch, handler):
    monkeypatch.setattr("sutr.api_client.validate_safe_url", lambda url, **kw: None)
    calls = {"n": 0}

    def counting(request):
        calls["n"] += 1
        return handler(request)

    class Patched(_REAL_ASYNC_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(counting)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.api_client.httpx.AsyncClient", Patched)
    return calls


async def _call(session, test_org):
    ctx = CallContext(org_id=test_org.id, source="api")
    gate = evaluate_gate(session, ctx, "customapi_provider", "fetch_thing", {})
    return await execute_tool(ctx, "customapi_provider", "fetch_thing", {}, gate)


def _refuse(request):
    raise httpx.ConnectError("connection refused")


# ── The provider stops being called ──────────────────────────────────────────


async def test_a_failing_provider_is_eventually_not_called_at_all(
    session, test_org, provider, monkeypatch
):
    calls = _answer_with(monkeypatch, _refuse)

    for _ in range(3):
        await _call(session, test_org)
    assert calls["n"] == 3
    assert breaker.health(HOST)["state"] == breaker.OPEN

    # The fourth call costs a dictionary lookup, not a connection attempt.
    outcome = await _call(session, test_org)
    assert calls["n"] == 3
    assert outcome.outcome == "error"


async def test_the_agent_is_told_why_rather_than_timing_out(
    session, test_org, provider, monkeypatch
):
    _answer_with(monkeypatch, _refuse)
    for _ in range(3):
        await _call(session, test_org)

    outcome = await _call(session, test_org)

    # The refusal is an error, not a result: nothing was executed, so nothing
    # is recorded as an execution.
    assert outcome.outcome == "error"
    assert "circuit is open" in outcome.error
    # And it says when to come back, so a retry is informed rather than a guess.
    assert "Retry in about" in outcome.error


async def test_a_recovered_provider_is_called_again(session, test_org, provider, monkeypatch):
    calls = _answer_with(monkeypatch, _refuse)
    for _ in range(3):
        await _call(session, test_org)

    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 0.0)
    calls.update(n=0)
    _answer_with(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))

    outcome = await _call(session, test_org)

    assert outcome.outcome == "executed", outcome.error
    assert breaker.health(HOST)["state"] == breaker.CLOSED


# ── What must not open a circuit ─────────────────────────────────────────────


async def test_a_404_from_a_working_provider_never_opens_the_circuit(
    session, test_org, provider, monkeypatch
):
    """A 404 is a provider working correctly and disagreeing with the request.
    Quarantining it would take a healthy provider away from every caller."""
    _answer_with(monkeypatch, lambda request: httpx.Response(404, json={"error": "not found"}))

    for _ in range(10):
        await _call(session, test_org)

    assert breaker.health(HOST)["state"] == breaker.CLOSED


async def test_repeated_500s_do_open_it(session, test_org, provider, monkeypatch):
    """A 5xx is the provider saying it is broken. That is its own health."""
    _answer_with(monkeypatch, lambda request: httpx.Response(503, text="unavailable"))

    for _ in range(3):
        await _call(session, test_org)

    assert breaker.health(HOST)["state"] == breaker.OPEN


async def test_an_unsafe_url_is_this_platforms_refusal_not_a_provider_failure(
    session, test_org, provider, monkeypatch
):
    """The call never left the process; blaming the provider for it would
    quarantine a provider that was never asked anything."""
    from sutr.upstream_safety import UnsafeUpstreamUrlError

    def refuse_url(url, **kwargs):
        raise UnsafeUpstreamUrlError("Host resolves to a blocked network range")

    monkeypatch.setattr("sutr.api_client.validate_safe_url", refuse_url)

    for _ in range(5):
        await _call(session, test_org)

    assert breaker.health(HOST)["state"] == breaker.CLOSED
    assert breaker.health(HOST)["failures"] == 0


# ── Retries at the pipeline level ────────────────────────────────────────────


async def test_a_get_that_fails_in_transport_is_retried_once(
    session, test_org, provider, monkeypatch
):
    monkeypatch.setattr(settings, "provider_retry_attempts", 2)
    monkeypatch.setattr(settings, "provider_retry_base_seconds", 0.0)
    attempts = {"n": 0}

    def flaky(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"ok": True})

    _answer_with(monkeypatch, flaky)

    outcome = await _call(session, test_org)

    assert outcome.outcome == "executed"
    assert attempts["n"] == 2
    # A retried call that succeeded is a success: the provider is not sick.
    assert breaker.health(HOST)["state"] == breaker.CLOSED


# ── The API ──────────────────────────────────────────────────────────────────


async def test_the_policy_endpoint_names_what_is_not_implemented(client):
    body = (await client.get("/v1/resilience/policy")).json()["data"]

    assert body["circuit_breaker"]["half_open_trial_calls"] == 1
    assert body["retries"]["retries"].startswith("transport failures")
    assert "bulkheads" in body["not_implemented"]
    assert len(body["domains"]) == 8
    assert set(body["severities"]) == {"P0", "P1", "P2", "P3"}


async def test_every_failure_domain_says_what_survives_it(client):
    """The half worth writing down: "the database is down" and "one provider is
    down" are the same sentence until you say what keeps working."""
    body = (await client.get("/v1/resilience/policy")).json()["data"]
    for domain in body["domains"]:
        assert domain["survives"].strip()
        assert domain["implemented_in"].strip()
        assert domain["severity"] in {"P0", "P1", "P2", "P3"}


async def test_the_providers_endpoint_reports_what_this_replica_quarantined(client):
    breaker.reset()
    for _ in range(3):
        breaker.record_failure(HOST, "refused")

    body = (await client.get("/v1/resilience/providers")).json()["data"]

    assert HOST in body["quarantined"]
    assert body["scope"] == "this replica only"


async def test_an_operator_can_close_a_circuit_and_it_is_audited(client, session, test_org):
    from sqlmodel import select

    from sutr.models.audit_event import AuditEvent

    breaker.reset()
    for _ in range(3):
        breaker.record_failure(HOST, "refused")

    response = await client.post(f"/v1/resilience/providers/{HOST}/close")

    assert response.status_code == 200
    assert breaker.health(HOST)["state"] == breaker.CLOSED
    recorded = session.exec(
        select(AuditEvent).where(AuditEvent.action == "resilience.circuit_closed")
    ).all()
    assert len(recorded) == 1
    assert recorded[0].target_id == HOST


async def test_closing_a_circuit_that_is_not_open_is_a_404(client):
    breaker.reset()
    assert (await client.post("/v1/resilience/providers/never.seen.example/close")).status_code == (
        404
    )
