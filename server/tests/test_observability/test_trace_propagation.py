"""Traces that span Gateway → Discovery → Runtime → Provider (ESDS LLD §5.3).

A span emitted in one process is not a distributed trace. What makes these
tests worth having is that they assert the *joins*: that a caller's trace
context is continued rather than replaced, that the four stages end up in one
trace with the right parents, that the context leaves again on the provider
call, and that a log line written underneath a span can be found from it.

They also pin what must never ride along. A span leaves the process, so tool
arguments, provider URLs (credentials can be in a query string) and search
intents stay out of it. Those assertions are the reason to prefer a test over
a comment.
"""

import json
import logging

import httpx
import pytest

from sutr.config import settings
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration import InstalledIntegration
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.observability import propagation
from sutr.observability.log_format import JsonFormatter
from sutr.observability.tracing import (
    _exporter_tls,
    _sampler,
    describe,
    mtls_configured,
    span,
    use_tracer,
)
from sutr.services.tool_pipeline import CallContext, evaluate_gate, execute_tool

# A well-known W3C example: trace 4bf9…, span 00f0…, sampled.
INBOUND_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
INBOUND_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INBOUND_SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture(name="span_capture")
def span_capture_fixture():
    """A real in-memory OTel tracer, uninstalled afterwards."""
    sdk_trace = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    use_tracer(provider.get_tracer("sutr-test"))
    try:
        yield exporter
    finally:
        use_tracer(None)


def _hex_trace(span_object) -> str:
    return format(span_object.get_span_context().trace_id, "032x")


def _by_name(exporter) -> dict:
    return {s.name: s for s in exporter.get_finished_spans()}


# ── The Gateway leg ──────────────────────────────────────────────────────────


async def test_a_request_carrying_trace_context_continues_that_trace(client, span_capture):
    await client.get("/v1/platform/capabilities", headers={"traceparent": INBOUND_TRACEPARENT})

    gateway = _by_name(span_capture)["sutr.gateway"]
    assert format(gateway.context.trace_id, "032x") == INBOUND_TRACE_ID
    # Parented to the caller's span, which is what stops a trace breaking into
    # one disconnected piece per hop.
    assert format(gateway.parent.span_id, "016x") == INBOUND_SPAN_ID


async def test_a_request_without_trace_context_starts_its_own(client, span_capture):
    await client.get("/v1/platform/capabilities")

    gateway = _by_name(span_capture)["sutr.gateway"]
    assert gateway.parent is None
    assert format(gateway.context.trace_id, "032x") != INBOUND_TRACE_ID


async def test_the_gateway_span_carries_the_route_template_and_the_status(client, span_capture):
    await client.get("/v1/platform/capabilities")

    attributes = dict(_by_name(span_capture)["sutr.gateway"].attributes)
    assert attributes["http.route"] == "/v1/platform/capabilities"
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200
    assert attributes["sutr.stage"] == "gateway"


async def test_the_gateway_span_records_the_status_of_a_failure(client, span_capture):
    await client.get("/v1/registry/tools/not-a-uuid")

    attributes = dict(_by_name(span_capture)["sutr.gateway"].attributes)
    assert attributes["http.response.status_code"] >= 400


async def test_the_scrape_endpoint_is_not_traced(client, span_capture, monkeypatch):
    """A span per scrape is noise that costs money and tells nobody anything."""
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "")

    await client.get("/metrics")
    await client.get("/health")

    assert span_capture.get_finished_spans() == ()


async def test_the_sse_stream_is_not_traced(client, span_capture, monkeypatch):
    """One request that stays open for hours would export one span, hours late,
    describing nothing that happened inside it."""
    monkeypatch.setattr(settings, "mcp_sse_enabled", False)

    await client.post("/sse/", json={})

    assert span_capture.get_finished_spans() == ()


# ── The Runtime and Provider legs ────────────────────────────────────────────


@pytest.fixture(name="http_provider")
def http_provider_fixture(session, test_org):
    """An installed custom API whose one tool makes a real outbound request."""
    tool = {
        "name": "fetch_thing",
        "description": "Fetch a thing",
        "method": "GET",
        "path": "/things",
        "params": [{"name": "q", "query": True, "description": "search"}],
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


@pytest.fixture(name="captured_requests")
def captured_requests_fixture(monkeypatch):
    """Answer every outbound provider request, keeping what was sent.

    The dispatch-time URL screen is stubbed out: it resolves the hostname for
    real, and a test that needs DNS is a test that fails on an aeroplane.
    """
    monkeypatch.setattr("sutr.api_client.validate_safe_url", lambda url, **kw: None)

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.api_client.httpx.AsyncClient", Patched)
    return sent


async def _call_the_provider(session, test_org, **args):
    ctx = CallContext(org_id=test_org.id, source="api")
    gate = evaluate_gate(session, ctx, "customapi_provider", "fetch_thing", args)
    return await execute_tool(ctx, "customapi_provider", "fetch_thing", args, gate)


async def test_one_tool_call_produces_gateway_runtime_and_provider_in_one_trace(
    session, test_org, http_provider, captured_requests, span_capture
):
    with span("sutr.gateway", kind="server", **{"sutr.stage": "gateway"}):
        await _call_the_provider(session, test_org)

    spans = _by_name(span_capture)
    assert set(spans) == {"sutr.gateway", "sutr.tool_call", "sutr.provider.request"}
    # One trace, three stages, in the order the LLD names them.
    assert len({s.context.trace_id for s in spans.values()}) == 1
    assert spans["sutr.tool_call"].parent.span_id == spans["sutr.gateway"].context.span_id
    assert spans["sutr.provider.request"].parent.span_id == spans["sutr.tool_call"].context.span_id
    assert [
        dict(spans[name].attributes)["sutr.stage"]
        for name in ("sutr.gateway", "sutr.tool_call", "sutr.provider.request")
    ] == [
        "gateway",
        "runtime",
        "provider",
    ]


async def test_the_provider_is_sent_the_trace_context_and_the_correlation_id(
    session, test_org, http_provider, captured_requests, span_capture
):
    from sutr.request_context import set_correlation_id

    set_correlation_id("corr-outbound")
    with span("sutr.gateway", kind="server"):
        await _call_the_provider(session, test_org)

    sent = captured_requests[0]
    assert sent.headers["x-correlation-id"] == "corr-outbound"
    # The traceparent names the trace the provider span belongs to, so the far
    # side parents onto this call rather than onto nothing.
    provider_span = _by_name(span_capture)["sutr.provider.request"]
    assert format(provider_span.context.trace_id, "032x") in sent.headers["traceparent"]


async def test_the_correlation_id_travels_even_with_tracing_off(
    session, test_org, http_provider, captured_requests
):
    """The default install has no OpenTelemetry SDK. It still correlates."""
    from sutr.request_context import set_correlation_id

    use_tracer(None)
    set_correlation_id("corr-no-tracing")
    await _call_the_provider(session, test_org)

    sent = captured_requests[0]
    assert sent.headers["x-correlation-id"] == "corr-no-tracing"
    assert "traceparent" not in sent.headers


async def test_the_provider_span_names_the_host_and_never_the_url(
    session, test_org, http_provider, captured_requests, span_capture
):
    """Credentials can ride in a query string (ADR-009), and a span leaves the
    process — so the URL does not go on it."""
    await _call_the_provider(session, test_org, q="token-shaped-argument")

    attributes = dict(_by_name(span_capture)["sutr.provider.request"].attributes)
    assert attributes["server.address"] == "api.provider.example"
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200
    assert "token-shaped-argument" not in str(attributes)
    assert not any("api.provider.example/" in str(value) for value in attributes.values())


async def test_an_upstream_error_is_recorded_on_the_provider_span(
    session, test_org, http_provider, monkeypatch, span_capture
):
    class Failing(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.api_client.validate_safe_url", lambda url, **kw: None)
    monkeypatch.setattr("sutr.api_client.httpx.AsyncClient", Failing)
    await _call_the_provider(session, test_org)

    provider_span = _by_name(span_capture)["sutr.provider.request"]
    assert any(event.name == "exception" for event in provider_span.events)


# ── The Discovery leg ────────────────────────────────────────────────────────


async def test_discovery_is_a_span_that_describes_shape_not_content(
    session, test_org, span_capture
):
    from sutr.discovery import service as discovery_service

    with span("sutr.gateway", kind="server"):
        await discovery_service.search(
            session, org_id=test_org.id, intent="refund a payment for a customer"
        )

    spans = _by_name(span_capture)
    discovery = spans["sutr.discovery.search"]
    assert discovery.parent.span_id == spans["sutr.gateway"].context.span_id
    attributes = dict(discovery.attributes)
    assert attributes["sutr.stage"] == "discovery"
    assert attributes["sutr.intent_length"] == len("refund a payment for a customer")
    assert "sutr.ranking_version" in attributes
    assert "sutr.stage.retrieval_ms" in attributes
    # The intent is a user's sentence. It never leaves the process.
    assert "refund" not in str(attributes)


# ── Correlation, and the join between a log line and a trace ─────────────────


async def test_every_span_carries_the_correlation_id(span_capture):
    from sutr.request_context import set_correlation_id

    set_correlation_id("corr-on-span")
    with span("sutr.anything"):
        pass

    assert dict(span_capture.get_finished_spans()[0].attributes)["sutr.correlation_id"] == (
        "corr-on-span"
    )


def test_a_log_line_written_inside_a_span_carries_that_trace_id(span_capture):
    """This is the Loki → Tempo join: the id on the line is the trace to open."""
    record = logging.LogRecord("sutr.test", logging.INFO, __file__, 1, "working", (), None)
    with span("sutr.work"):
        payload = json.loads(JsonFormatter().format(record))

    finished = span_capture.get_finished_spans()[0]
    assert payload["trace_id"] == format(finished.context.trace_id, "032x")
    assert payload["span_id"] == format(finished.context.span_id, "016x")


def test_a_log_line_written_outside_a_trace_claims_no_trace_id():
    """A line naming a trace that no backend holds sends the reader nowhere."""
    use_tracer(None)
    record = logging.LogRecord("sutr.test", logging.INFO, __file__, 1, "working", (), None)
    payload = json.loads(JsonFormatter().format(record))
    assert "trace_id" not in payload


def test_the_region_comes_from_configuration_rather_than_from_the_call_site(monkeypatch):
    monkeypatch.setattr(settings, "region", "ap-south-1")
    record = logging.LogRecord("sutr.test", logging.INFO, __file__, 1, "working", (), None)
    assert json.loads(JsonFormatter().format(record))["region"] == "ap-south-1"


def test_an_unset_region_is_null_rather_than_a_guess(monkeypatch):
    monkeypatch.setattr(settings, "region", "")
    record = logging.LogRecord("sutr.test", logging.INFO, __file__, 1, "working", (), None)
    assert json.loads(JsonFormatter().format(record))["region"] is None


# ── Propagation without the SDK, and the sampler ─────────────────────────────


def test_propagation_degrades_to_the_correlation_id_alone(monkeypatch):
    """The base install carries no OpenTelemetry package at all."""
    from sutr.request_context import set_correlation_id

    monkeypatch.setattr(propagation, "_propagator", None)
    monkeypatch.setattr(propagation, "_propagator_loaded", True)
    set_correlation_id("corr-no-sdk")

    headers = propagation.outbound_headers({"Authorization": "Bearer x"})
    assert headers == {"Authorization": "Bearer x", "X-Correlation-ID": "corr-no-sdk"}
    assert propagation.extract({"traceparent": INBOUND_TRACEPARENT}) is None


def test_a_caller_that_was_sampled_keeps_being_sampled_here():
    """ParentBased: otherwise a cross-service trace comes back with holes."""
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.sampling import Decision
    from opentelemetry.trace import SpanKind

    with pytest.MonkeyPatch.context() as patch_context:
        patch_context.setattr(settings, "otel_traces_sample_ratio", 0.0)
        sampler = _sampler()

    remote = propagation.extract({"traceparent": INBOUND_TRACEPARENT})
    decision = sampler.should_sample(remote, 0x1234, "sutr.gateway", SpanKind.SERVER)
    assert decision.decision is not Decision.DROP

    # And with no parent at all, a ratio of zero really does drop.
    rootless = sampler.should_sample(None, 0x1234, "sutr.gateway", SpanKind.SERVER)
    assert rootless.decision is Decision.DROP


# ── mTLS to the collector ────────────────────────────────────────────────────


def test_the_exporter_is_handed_the_configured_tls_material(monkeypatch):
    monkeypatch.setattr(settings, "otel_exporter_otlp_certificate", "/certs/ca.crt")
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_certificate", "/certs/client.crt")
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_key", "/certs/client.key")

    # The names are the OTLP exporter's own keyword arguments.
    assert _exporter_tls() == {
        "certificate_file": "/certs/ca.crt",
        "client_certificate_file": "/certs/client.crt",
        "client_key_file": "/certs/client.key",
    }


def test_unset_tls_material_is_omitted_rather_than_passed_as_empty(monkeypatch):
    monkeypatch.setattr(settings, "otel_exporter_otlp_certificate", "")
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_certificate", "")
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_key", "")
    assert _exporter_tls() == {}


def test_half_a_client_certificate_is_not_reported_as_mutual_tls(monkeypatch):
    """An operator would otherwise learn the truth from a collector's reject log."""
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_certificate", "/certs/client.crt")
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_key", "")
    assert mtls_configured() is False

    monkeypatch.setattr(settings, "otel_exporter_otlp_client_key", "/certs/client.key")
    assert mtls_configured() is True


def test_the_tracing_report_says_what_is_actually_configured(monkeypatch):
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "")
    monkeypatch.setattr(settings, "otel_exporter_otlp_certificate", "")
    monkeypatch.setattr(settings, "otel_exporter_otlp_client_certificate", "")

    reported = describe()
    assert reported["exporter_endpoint"] is None
    assert reported["collector_tls"] is False
    assert reported["collector_mtls"] is False
    assert reported["propagation"] == "w3c-tracecontext"
