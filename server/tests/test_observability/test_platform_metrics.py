"""The metrics LLD §5.3.5 names, and the two properties they must keep.

§5.3.5 asks for request rate, latency, errors, provider latency, queue depth
and lag. Rate, latency and errors were already exported; provider latency and
the async-path gauges are added here.

The two properties, both asserted below:

1. **No series identifies a tenant.** `/metrics` is scraped by infrastructure,
   not by tenants, and one series per organisation would be both a cardinality
   problem and a disclosure. Per-tenant numbers are served, org-scoped, by
   `/v1/observability`.
2. **A gauge that could not be sampled reads as stale, not as zero.** Zero is a
   number an alert acts on; stale is a state the next sample corrects.
"""

import json

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from sutr.config import settings
from sutr.events import outbox
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration import InstalledIntegration
from sutr.models.outbox_event import DEAD_LETTERED
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.observability import collectors, metrics
from sutr.services.tool_pipeline import CallContext, evaluate_gate, execute_tool


def _sample(body: str, name: str, **labels) -> float | None:
    for family in text_string_to_metric_families(body):
        for s in family.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


def _rendered() -> str:
    return metrics.render()[0].decode()


# ── The provider hop ─────────────────────────────────────────────────────────


@pytest.fixture(name="http_provider")
def http_provider_fixture(session, test_org):
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
            base_url="https://api.provider.example",
            tools_json=json.dumps([tool]),
        )
    )
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="customapi_provider",
            type="custom",
            url="https://api.provider.example",
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


def _answer_with(monkeypatch, response_factory):
    monkeypatch.setattr("sutr.api_client.validate_safe_url", lambda url, **kw: None)

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(response_factory)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.api_client.httpx.AsyncClient", Patched)


async def _call(session, test_org):
    ctx = CallContext(org_id=test_org.id, source="api")
    gate = evaluate_gate(session, ctx, "customapi_provider", "fetch_thing", {})
    return await execute_tool(ctx, "customapi_provider", "fetch_thing", {}, gate)


async def test_a_provider_call_is_counted_with_its_latency(
    session, test_org, http_provider, monkeypatch
):
    before = _sample(_rendered(), "sutr_provider_requests_total", transport="http", outcome="ok")
    _answer_with(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))

    await _call(session, test_org)

    body = _rendered()
    assert (
        _sample(body, "sutr_provider_requests_total", transport="http", outcome="ok")
        == (before or 0.0) + 1
    )
    assert _sample(body, "sutr_provider_request_duration_seconds_count", transport="http")


async def test_an_upstream_error_status_is_counted_as_an_error(
    session, test_org, http_provider, monkeypatch
):
    """A 500 from a provider is a failed provider call even though the tool
    call itself returned a result to the agent."""
    before = (
        _sample(_rendered(), "sutr_provider_requests_total", transport="http", outcome="error")
        or 0.0
    )
    _answer_with(monkeypatch, lambda request: httpx.Response(503, text="upstream is down"))

    await _call(session, test_org)

    assert (
        _sample(_rendered(), "sutr_provider_requests_total", transport="http", outcome="error")
        == before + 1
    )


async def test_a_connection_failure_is_counted_before_it_is_raised(
    session, test_org, http_provider, monkeypatch
):
    before = (
        _sample(_rendered(), "sutr_provider_requests_total", transport="http", outcome="error")
        or 0.0
    )

    def refuse(request):
        raise httpx.ConnectError("refused")

    _answer_with(monkeypatch, refuse)
    await _call(session, test_org)

    assert (
        _sample(_rendered(), "sutr_provider_requests_total", transport="http", outcome="error")
        == before + 1
    )


# ── The async path ───────────────────────────────────────────────────────────


def test_queue_depth_and_lag_are_sampled_from_the_outbox(session):
    outbox.publish(session, "tool.registered", resource_id="a")
    outbox.publish(session, "tool.registered", resource_id="b")
    session.commit()

    sampled = collectors.sample(session)

    assert sampled["outbox_pending"] == 2
    body = _rendered()
    assert _sample(body, "sutr_queue_depth", queue="outbox_pending") == 2
    # Two rows just written are seconds old at most, but they are not negative
    # and they are not zero-because-nothing-is-waiting.
    assert sampled["relay_lag_seconds"] >= 0


def test_a_dead_lettered_event_is_counted_in_its_own_queue(session):
    row = outbox.publish(session, "tool.registered", resource_id="c")
    session.commit()
    row.state = DEAD_LETTERED
    session.add(row)
    session.commit()

    collectors.sample(session)

    body = _rendered()
    assert _sample(body, "sutr_queue_depth", queue="outbox_dead_letter") == 1
    assert _sample(body, "sutr_queue_depth", queue="outbox_pending") == 0


def test_an_empty_queue_reports_no_lag_rather_than_the_age_of_the_epoch(session):
    sampled = collectors.sample(session)

    assert sampled["outbox_pending"] == 0
    assert sampled["relay_lag_seconds"] == 0.0
    assert _sample(_rendered(), "sutr_event_relay_lag_seconds") == 0.0


def test_lag_is_the_age_of_the_oldest_waiting_event(session):
    from datetime import datetime, timedelta

    row = outbox.publish(session, "tool.registered", resource_id="d")
    session.commit()
    row.created_at = datetime.utcnow() - timedelta(seconds=600)
    session.add(row)
    session.commit()

    assert collectors.sample(session)["relay_lag_seconds"] >= 600


def test_a_sample_that_cannot_read_the_database_is_counted_and_the_gauges_left_alone(
    session, monkeypatch
):
    """The graceful-degradation path: a monitoring endpoint that falls over
    with the database takes away the metrics that would explain the outage."""
    outbox.publish(session, "tool.registered", resource_id="e")
    session.commit()
    collectors.sample(session)
    assert _sample(_rendered(), "sutr_queue_depth", queue="outbox_pending") == 1

    failures_before = _sample(_rendered(), "sutr_telemetry_sample_failures_total") or 0.0

    def explode(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(session, "exec", explode)
    result = collectors.sample(session)

    assert result == {"sampled": False}
    body = _rendered()
    assert _sample(body, "sutr_telemetry_sample_failures_total") == failures_before + 1
    # Stale, not zero. Zero would say the queue had drained.
    assert _sample(body, "sutr_queue_depth", queue="outbox_pending") == 1


async def test_the_scrape_endpoint_samples_the_gauges(client, session, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "")
    outbox.publish(session, "tool.registered", resource_id="f")
    session.commit()

    body = (await client.get("/metrics")).text

    assert _sample(body, "sutr_queue_depth", queue="outbox_pending") == 1


# ── What may never appear in a shared metrics surface ────────────────────────


async def test_no_series_carries_a_tenant_identifier(session, test_org, http_provider, monkeypatch):
    _answer_with(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))
    await _call(session, test_org)
    collectors.sample(session)

    body = _rendered()
    assert str(test_org.id) not in body
    forbidden = {"org", "org_id", "tenant", "tenant_id", "user", "user_id", "api_key"}
    for family in text_string_to_metric_families(body):
        for series in family.samples:
            assert not forbidden & set(series.labels), f"{series.name} labels a tenant"


def test_the_provider_series_does_not_name_the_provider():
    """Which providers a platform calls, and how often, is commercially
    sensitive; /metrics is scraped by infrastructure that is not the tenant."""
    metrics.observe_provider_request("http", "ok", 12)
    for family in text_string_to_metric_families(_rendered()):
        for series in family.samples:
            if series.name.startswith("sutr_provider_"):
                # `le` is the histogram's own bucket boundary, not a dimension
                # anybody chose.
                assert set(series.labels) <= {"transport", "outcome", "le"}


def test_the_outbox_gauge_counts_rows_rather_than_reading_their_contents(session):
    """A queue-depth gauge that read payloads would put tenant data on a shared
    surface by accident. It counts, and the count carries no labels but its own."""
    outbox.publish(session, "tool.registered", tenant_id="tenant-a", resource_id="g")
    session.commit()
    collectors.sample(session)

    body = _rendered()
    assert "tenant-a" not in body
    assert _sample(body, "sutr_queue_depth", queue="outbox_pending") == 1


def test_sampling_reads_only_the_two_columns_it_needs(session):
    """Pinned deliberately: the sample runs on every scrape, and a scrape that
    loads whole rows would be a scrape that pulls envelopes into memory."""
    import inspect

    source = inspect.getsource(collectors.sample)
    assert "OutboxEvent.state" in source
    assert "OutboxEvent.created_at" in source
    assert "select(OutboxEvent)" not in source
