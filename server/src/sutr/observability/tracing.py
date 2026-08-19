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

    resource = Resource.create(
        {
            SERVICE_NAME: settings.otel_service_name,
            SERVICE_VERSION: "0.1.0",
        }
    )
    provider = TracerProvider(resource=resource)

    endpoint = settings.otel_exporter_otlp_endpoint
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
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


def use_tracer(tracer) -> None:
    """Install a tracer directly. Used by tests to capture spans in memory."""
    global _tracer, _enabled
    _tracer = tracer
    _enabled = tracer is not None


def tracing_enabled() -> bool:
    return _enabled


@contextmanager
def span(name: str, **attributes):
    """Start a span when tracing is on; otherwise a zero-cost no-op.

    Yields an object with `set_attribute`/`set_attributes` when active and None
    when inactive, so call sites guard with `if s is not None`.
    """
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as active_span:
        for key, value in attributes.items():
            if value is not None:
                active_span.set_attribute(key, value)
        yield active_span
