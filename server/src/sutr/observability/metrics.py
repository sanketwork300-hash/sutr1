"""Prometheus metrics for the Sutr control plane.

Deliberate design decision — **no org/tenant labels anywhere**. Two reasons:
1. Cardinality: one series per org per tool would explode a shared registry.
2. Disclosure: /metrics is scraped by infrastructure, not by tenants; org ids
   and tool names of other tenants must not leak through it.
Per-tenant usage is metered durably in `usage_event` and served, org-scoped and
permission-checked, by `/api/usage`.

Route labels use the matched *route template* (e.g. /api/tools/{integration_id}/call),
never the raw path, so ids never become label values.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# A dedicated registry rather than the global default: tests can build a fresh
# module state, and we never accidentally export another library's collectors.
REGISTRY = CollectorRegistry()

TOOL_CALLS = Counter(
    "sutr_tool_calls_total",
    "Tool executions attempted through the canonical pipeline.",
    ["source", "outcome"],
    registry=REGISTRY,
)

TOOL_CALL_DURATION = Histogram(
    "sutr_tool_call_duration_seconds",
    "Upstream tool execution latency.",
    ["source"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

TOOL_CALLS_GATED = Counter(
    "sutr_tool_calls_gated_total",
    "Tool calls stopped before execution by policy.",
    ["source", "reason"],  # reason: denied | approval_pending
    registry=REGISTRY,
)

APPROVAL_DECISIONS = Counter(
    "sutr_approval_decisions_total",
    "Human decisions recorded on approval requests.",
    ["decision"],  # approve_once | approve_exact_forever | allow_tool_forever | deny
    registry=REGISTRY,
)

HTTP_REQUESTS = Counter(
    "sutr_http_requests_total",
    "HTTP requests served.",
    ["method", "route", "status"],
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION = Histogram(
    "sutr_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=(0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

DEPLOYMENTS = Gauge(
    "sutr_deployments",
    "Deployments by status, as last observed by the monitoring sweep.",
    ["status"],
    registry=REGISTRY,
)


def observe_tool_call(source: str, outcome: str, duration_ms: int) -> None:
    TOOL_CALLS.labels(source=source, outcome=outcome).inc()
    TOOL_CALL_DURATION.labels(source=source).observe(duration_ms / 1000.0)


def observe_tool_gated(source: str, reason: str) -> None:
    TOOL_CALLS_GATED.labels(source=source, reason=reason).inc()


def observe_approval_decision(decision: str) -> None:
    APPROVAL_DECISIONS.labels(decision=decision).inc()


def observe_http_request(method: str, route: str, status: int, duration_s: float) -> None:
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(duration_s)


def set_deployment_counts(counts: dict[str, int]) -> None:
    """Replace the deployment gauge series. Absent statuses are zeroed so a
    drained status does not keep reporting its last value."""
    for status in ("queued", "building", "running", "stopped", "failed"):
        DEPLOYMENTS.labels(status=status).set(counts.get(status, 0))


def render() -> tuple[bytes, str]:
    """(body, content_type) for the /metrics response."""
    from prometheus_client import CONTENT_TYPE_LATEST

    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
