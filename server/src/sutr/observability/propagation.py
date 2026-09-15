"""Carrying one operation's identity across a network hop.

The ESDS LLD §5.3 asks for traces that span *Gateway → Discovery → Runtime →
Provider*. Spans alone do not do that: a span emitted in this process joins a
caller's trace only if the caller's trace context arrived with the request, and
a provider's span joins ours only if we send ours on. That is what this module
does — extract on the way in, inject on the way out — using the W3C Trace
Context headers (`traceparent`, `tracestate`), which are the interoperable
standard rather than anything specific to this platform.

Two things propagate, and they are deliberately independent:

- **The correlation id** always propagates. It is ours, it costs nothing, and
  it works with the OpenTelemetry SDK absent — which is the default install.
- **The trace context** propagates only when tracing is actually running.
  Emitting a `traceparent` for a trace nobody is recording would invite a
  provider to parent its spans onto an id that leads nowhere.
"""

from sutr.request_context import get_correlation_id

CORRELATION_HEADER = "X-Correlation-ID"
TRACEPARENT_HEADER = "traceparent"

_propagator = None
_propagator_loaded = False


def _get_propagator():
    """The W3C propagator, or None when the OpenTelemetry API is not installed."""
    global _propagator, _propagator_loaded
    if not _propagator_loaded:
        _propagator_loaded = True
        try:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )
        except ImportError:
            _propagator = None
        else:
            _propagator = TraceContextTextMapPropagator()
    return _propagator


def extract(headers) -> object | None:
    """The remote context carried by `headers`, or None if there is none.

    `headers` is any mapping of header name to value; names are matched
    case-insensitively, as HTTP requires.
    """
    propagator = _get_propagator()
    if propagator is None:
        return None
    carrier = {str(name).lower(): value for name, value in dict(headers).items()}
    if TRACEPARENT_HEADER not in carrier:
        return None
    context = propagator.extract(carrier)
    return context or None


def outbound_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers to add to an outbound call so the far side joins this operation.

    Always carries the correlation id. Carries `traceparent`/`tracestate` only
    when a span is currently recording, so a disabled or unsampled trace sends
    nothing rather than a dead reference.
    """
    headers: dict[str, str] = dict(extra or {})
    correlation_id = get_correlation_id()
    if correlation_id:
        headers[CORRELATION_HEADER] = correlation_id

    propagator = _get_propagator()
    if propagator is not None:
        carrier: dict[str, str] = {}
        propagator.inject(carrier)
        headers.update(carrier)
    return headers


def current_trace_id() -> str | None:
    """The active trace id as 32 lowercase hex characters, or None.

    This is what joins a log line to a trace: Loki holds the line, Tempo holds
    the trace, and the id in the line is the link between them.
    """
    ids = _current_ids()
    return ids[0] if ids else None


def current_span_id() -> str | None:
    ids = _current_ids()
    return ids[1] if ids else None


def _current_ids() -> tuple[str, str] | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return (
        format(span_context.trace_id, "032x"),
        format(span_context.span_id, "016x"),
    )
