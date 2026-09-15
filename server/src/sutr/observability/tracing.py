"""Optional OpenTelemetry tracing.

Off by default and dependency-optional: the OTel SDK lives in the `otel` extra
(`uv sync --extra otel`). With tracing disabled — or the SDK absent — every
helper here is a cheap no-op, so instrumented call sites need no conditionals
and the base install carries no OTel dependency.

Spans are emitted for the operations worth tracing across a distributed call:
tool execution (including the upstream HTTP hop) and the approval long-poll.
Span attributes carry integration/tool/outcome but never arguments, results,
or credentials — traces leave the process, and tool arguments can contain
anything the agent passed in.
"""

import logging
from contextlib import contextmanager

from sutr.config import settings
from sutr.request_context import get_correlation_id

logger = logging.getLogger(__name__)

_tracer = None
_enabled = False


def configure_tracing() -> bool:
    """Initialise tracing if enabled and the SDK is installed. Returns active state."""
    global _tracer, _enabled
    if not settings.otel_enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_ENABLED is set but the OpenTelemetry SDK is not installed. "
            "Install the extra: uv sync --extra otel"
        )
        return False

    attributes = {
        SERVICE_NAME: settings.otel_service_name,
        SERVICE_VERSION: "0.1.0",
    }
    # The region is a resource attribute rather than a span attribute: it
    # describes the process emitting the span, not the operation, and putting
    # it on the resource is what lets a multi-region backend group by it.
    if settings.region:
        attributes["cloud.region"] = settings.region
    resource = Resource.create(attributes)
    provider = TracerProvider(resource=resource, sampler=_sampler())

    endpoint = settings.otel_exporter_otlp_endpoint
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, **_exporter_tls()))
            )
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP HTTP exporter is not "
                "installed; spans will be recorded without being exported."
            )
    else:
        logger.info("Tracing enabled without an OTLP endpoint — spans are recorded only.")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("sutr")
    _enabled = True
    logger.info("OpenTelemetry tracing enabled (service=%s)", settings.otel_service_name)
    return True


def _sampler():
    """Head sampling, respecting an upstream caller's decision.

    ParentBased means a request that arrived already sampled stays sampled all
    the way through this process — otherwise a trace that spans Gateway →
    Runtime → Provider would come back with holes in it, which is precisely the
    trace worth having.
    """
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    ratio = settings.otel_traces_sample_ratio
    ratio = 1.0 if ratio is None else max(0.0, min(1.0, float(ratio)))
    return ParentBased(root=TraceIdRatioBased(ratio))


def _exporter_tls() -> dict:
    """TLS material for the OTLP exporter, in the exporter's own argument names.

    A CA bundle alone verifies the collector; adding a client certificate and
    key makes the hop mutual, which is what LLD §5.3 asks for between services
    and collectors. Unset values are omitted rather than passed as empty
    strings, so the exporter falls back to system trust.
    """
    options = {
        "certificate_file": settings.otel_exporter_otlp_certificate,
        "client_certificate_file": settings.otel_exporter_otlp_client_certificate,
        "client_key_file": settings.otel_exporter_otlp_client_key,
    }
    return {name: value for name, value in options.items() if value}


def mtls_configured() -> bool:
    """True when both halves of a client certificate are configured.

    One half is not mutual TLS, and reporting it as such would be a lie an
    operator would only discover from a collector's rejection log.
    """
    return bool(
        settings.otel_exporter_otlp_client_certificate and settings.otel_exporter_otlp_client_key
    )


def describe() -> dict:
    """What tracing is doing right now, for the capabilities report."""
    return {
        "enabled": _enabled,
        "service_name": settings.otel_service_name,
        "exporter_endpoint": settings.otel_exporter_otlp_endpoint or None,
        "sample_ratio": settings.otel_traces_sample_ratio,
        "collector_tls": bool(settings.otel_exporter_otlp_certificate),
        "collector_mtls": mtls_configured(),
        "propagation": "w3c-tracecontext",
    }


def use_tracer(tracer) -> None:
    """Install a tracer directly. Used by tests to capture spans in memory."""
    global _tracer, _enabled
    _tracer = tracer
    _enabled = tracer is not None


def tracing_enabled() -> bool:
    return _enabled


@contextmanager
def span(name: str, *, context=None, kind: str | None = None, **attributes):
    """Start a span when tracing is on; otherwise a zero-cost no-op.

    Yields an object with `set_attribute`/`set_attributes` when active and None
    when inactive, so call sites guard with `if s is not None`.

    `context` is a remote parent extracted from an inbound request
    (`observability.propagation.extract`); passing it is what makes this span a
    continuation of the caller's trace rather than the root of a new one.
    `kind` is the span kind as a lowercase string — "server" for a request this
    process is answering, "client" for one it is making — kept as a string so
    call sites need no OpenTelemetry import.

    Every span carries the correlation id. That is the one attribute worth
    setting unconditionally: it is the join between a trace in Tempo and the
    log lines in Loki that were emitted underneath it.
    """
    if not _enabled or _tracer is None:
        yield None
        return
    correlation_id = get_correlation_id()
    # `kind` is omitted rather than passed as None so the SDK's own default
    # (INTERNAL) applies; passing None would leave a null kind on the span.
    options = {"context": context}
    if kind is not None:
        options["kind"] = _span_kind(kind)
    with _tracer.start_as_current_span(name, **options) as active_span:
        if correlation_id:
            active_span.set_attribute("sutr.correlation_id", correlation_id)
        for key, value in attributes.items():
            if value is not None:
                active_span.set_attribute(key, value)
        yield active_span


def _span_kind(kind: str | None):
    if kind is None:
        return None
    from opentelemetry.trace import SpanKind

    return {
        "server": SpanKind.SERVER,
        "client": SpanKind.CLIENT,
        "internal": SpanKind.INTERNAL,
        "producer": SpanKind.PRODUCER,
        "consumer": SpanKind.CONSUMER,
    }[kind]
