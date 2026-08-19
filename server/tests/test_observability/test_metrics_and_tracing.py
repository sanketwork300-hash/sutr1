"""Prometheus metrics endpoint/labels and real OpenTelemetry spans."""

from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client.parser import text_string_to_metric_families

from sutr.config import settings
from sutr.models.integration import InstalledIntegration
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.observability import metrics
from sutr.observability.tracing import span, use_tracer
from sutr.services.tool_pipeline import CallContext, evaluate_gate, execute_tool

MOCK_RESULT = {"content": [{"type": "text", "text": "done"}]}


def _sample(body: str, name: str, **labels) -> float | None:
    for family in text_string_to_metric_families(body):
        for s in family.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


@pytest.fixture(name="metrics_on")
def metrics_on_fixture(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "")


@pytest.fixture(name="allowed_posthog")
def allowed_posthog_fixture(session, test_org):
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="posthog",
            type="remote_mcp",
            url="https://mcp.posthog.com/mcp",
            auth_method="token",
        )
    )
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id,
            integration_id="posthog",
            tool_name="create_annotation",
            mode="allow",
        )
    )
    session.commit()


# ── /metrics exposure and protection ─────────────────────────────────────────


async def test_metrics_disabled_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", False)
    resp = await client.get("/metrics")
    assert resp.status_code == 404  # existence not advertised


async def test_metrics_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", "scrape-secret")

    assert (await client.get("/metrics")).status_code == 401
    assert (
        await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    resp = await client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert resp.status_code == 200
    assert "sutr_tool_calls_total" in resp.text


async def test_metrics_exposes_no_tenant_labels(client, session, test_org, metrics_on):
    """Cardinality + disclosure guard: no org/tool identifiers in metrics."""
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert str(test_org.id) not in resp.text
    for family in text_string_to_metric_families(resp.text):
        for sample in family.samples:
            assert "org" not in sample.labels
            assert "integration_id" not in sample.labels
            assert "tool_name" not in sample.labels


async def test_http_metrics_use_route_templates_not_paths(client, session, test_org, metrics_on):
    await client.get("/api/logs")
    resp = await client.get("/metrics")

    assert (
        _sample(
            resp.text, "sutr_http_requests_total", route="/api/logs", method="GET", status="200"
        )
        is not None
    )
    # An id-bearing route must be labelled by its template, never the raw path.
    await client.get("/api/deployments/00000000-0000-0000-0000-000000000000")
    body = (await client.get("/metrics")).text
    assert "00000000-0000-0000-0000-000000000000" not in body
    assert (
        _sample(
            body,
            "sutr_http_requests_total",
            route="/api/deployments/{deployment_id}",
            method="GET",
            status="404",
        )
        is not None
    )


async def test_tool_call_metrics_increment(client, session, test_org, allowed_posthog, metrics_on):
    before = (
        _sample(
            (await client.get("/metrics")).text,
            "sutr_tool_calls_total",
            source="api",
            outcome="executed",
        )
        or 0.0
    )

    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=MOCK_RESULT):
        resp = await client.post(
            "/api/tools/posthog/call",
            json={"tool_name": "create_annotation", "args": {"content": "hi"}},
        )
    assert resp.status_code == 200

    body = (await client.get("/metrics")).text
    after = _sample(body, "sutr_tool_calls_total", source="api", outcome="executed")
    assert after == before + 1
    assert _sample(body, "sutr_tool_call_duration_seconds_count", source="api") is not None


async def test_gated_and_denied_calls_are_counted(client, session, test_org, metrics_on):
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="posthog",
            type="remote_mcp",
            url="https://mcp.posthog.com/mcp",
            auth_method="token",
        )
    )
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id, integration_id="posthog", tool_name="nuke", mode="deny"
        )
    )
    session.commit()

    before_pending = (
        _sample(
            (await client.get("/metrics")).text,
            "sutr_tool_calls_gated_total",
            source="api",
            reason="approval_pending",
        )
        or 0.0
    )

    await client.post(
        "/api/tools/posthog/call", json={"tool_name": "create_annotation", "args": {}}
    )
    await client.post("/api/tools/posthog/call", json={"tool_name": "nuke", "args": {}})

    body = (await client.get("/metrics")).text
    assert (
        _sample(body, "sutr_tool_calls_gated_total", source="api", reason="approval_pending")
        == before_pending + 1
    )
    assert _sample(body, "sutr_tool_calls_gated_total", source="api", reason="denied") is not None


def test_deployment_gauge_zeroes_drained_statuses():
    metrics.set_deployment_counts({"running": 2, "failed": 1})
    body = metrics.render()[0].decode()
    assert _sample(body, "sutr_deployments", status="running") == 2
    assert _sample(body, "sutr_deployments", status="stopped") == 0

    metrics.set_deployment_counts({})
    body = metrics.render()[0].decode()
    assert _sample(body, "sutr_deployments", status="running") == 0


# ── OpenTelemetry tracing (real SDK) ─────────────────────────────────────────


@pytest.fixture(name="span_capture")
def span_capture_fixture():
    """Install a real in-memory OTel tracer; uninstall afterwards."""
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


def test_span_is_noop_when_tracing_disabled():
    use_tracer(None)
    with span("sutr.tool_call", **{"sutr.tool_name": "x"}) as active:
        assert active is None  # call sites stay valid with tracing off


async def test_tool_execution_emits_span_with_safe_attributes(
    session, test_org, allowed_posthog, span_capture
):
    ctx = CallContext(org_id=test_org.id, source="mcp")
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=MOCK_RESULT):
        await execute_tool(ctx, "posthog", "create_annotation", {"content": "secret-value"}, gate)

    spans = span_capture.get_finished_spans()
    assert [s.name for s in spans] == ["sutr.tool_call"]
    attrs = dict(spans[0].attributes)
    assert attrs["sutr.integration_id"] == "posthog"
    assert attrs["sutr.tool_name"] == "create_annotation"
    assert attrs["sutr.source"] == "mcp"
    assert attrs["sutr.outcome"] == "executed"
    assert attrs["sutr.access_reason"] == "approved_any"
    assert "sutr.duration_ms" in attrs
    # Traces leave the process: arguments and results must never ride along.
    assert "secret-value" not in str(attrs)


async def test_failed_execution_records_exception_on_span(
    session, test_org, allowed_posthog, span_capture
):
    ctx = CallContext(org_id=test_org.id, source="api")
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    with patch(
        "sutr.mcp.client.call_tool",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upstream exploded"),
    ):
        await execute_tool(ctx, "posthog", "create_annotation", {"content": "hi"}, gate)

    finished = span_capture.get_finished_spans()[0]
    assert dict(finished.attributes)["sutr.outcome"] == "error"
    assert any(event.name == "exception" for event in finished.events)


def test_configure_tracing_is_inert_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "otel_enabled", False)
    from sutr.observability import tracing

    assert tracing.configure_tracing() is False


def test_configure_tracing_activates_without_exporter(monkeypatch):
    """OTEL_ENABLED with no endpoint records spans locally rather than failing."""
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "")
    from sutr.observability import tracing

    try:
        assert tracing.configure_tracing() is True
        assert tracing.tracing_enabled() is True
    finally:
        use_tracer(None)
