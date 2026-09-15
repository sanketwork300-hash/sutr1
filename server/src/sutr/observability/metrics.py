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
    ["source", "reason"],  # reason: denied | approval_pending | rate_limited | quota_exceeded
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

# ── The provider hop ─────────────────────────────────────────────────────────
# LLD §5.3.5 asks for provider latency. Labelled by transport and outcome, not
# by provider: which providers a platform calls, and how often, is commercially
# sensitive and /metrics is scraped by infrastructure. Per-provider latency is
# available to the tenant that owns the call through the tenant telemetry API.

PROVIDER_REQUESTS = Counter(
    "sutr_provider_requests_total",
    "Requests made to provider systems on behalf of an agent.",
    ["transport", "outcome"],  # transport: http | mcp — outcome: ok | error
    registry=REGISTRY,
)

PROVIDER_REQUEST_DURATION = Histogram(
    "sutr_provider_request_duration_seconds",
    "Provider response latency, measured at this platform's socket.",
    ["transport"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

# ── The async path ───────────────────────────────────────────────────────────
# The LLD's §5.3.5 list names queue depth, Kafka lag and billing lag. In this
# install every asynchronous fact — usage included — leaves through the
# transactional outbox, so the depth of that queue and the age of its oldest
# unpublished row *are* those three numbers. Naming them after the outbox
# rather than after Kafka keeps the metric honest on an install that has no
# broker at all.

QUEUE_DEPTH = Gauge(
    "sutr_queue_depth",
    "Rows waiting in an internal queue, as of the last sample.",
    ["queue"],  # outbox_pending | outbox_dead_letter
    registry=REGISTRY,
)

RELAY_LAG = Gauge(
    "sutr_event_relay_lag_seconds",
    "Age of the oldest event still waiting to be published. 0 when none waits.",
    registry=REGISTRY,
)

TELEMETRY_SAMPLE_FAILURES = Counter(
    "sutr_telemetry_sample_failures_total",
    "Scrapes where the database-backed gauges could not be sampled.",
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


def observe_provider_request(transport: str, outcome: str, duration_ms: int) -> None:
    PROVIDER_REQUESTS.labels(transport=transport, outcome=outcome).inc()
    PROVIDER_REQUEST_DURATION.labels(transport=transport).observe(duration_ms / 1000.0)


def set_queue_depth(queue: str, depth: int) -> None:
    QUEUE_DEPTH.labels(queue=queue).set(depth)


def set_relay_lag_seconds(seconds: float) -> None:
    RELAY_LAG.set(seconds)


def record_sample_failure() -> None:
    """A scrape that could not read the database.

    Counted rather than raised: a scrape that fails outright takes the process
    metrics down with it, and the gauges it could not refresh are worth less
    than the counters it would have hidden.
    """
    TELEMETRY_SAMPLE_FAILURES.inc()


def render() -> tuple[bytes, str]:
    """(body, content_type) for the /metrics response."""
    from prometheus_client import CONTENT_TYPE_LATEST

    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
