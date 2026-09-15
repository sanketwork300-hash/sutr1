"""Per-request/per-call fields every log line carries.

ESDS LLD §5.3 requires each log line to carry correlation id, agent id,
provider id, tool id, runtime id, status and duration; the build prompt adds
tenant id and region. Rather than threading a dozen arguments through every
call site — which guarantees they get forgotten — the fields live in a context
variable that the formatter reads. Middleware and the execution pipeline bind
them once; every log emitted underneath inherits them.

The field set is fixed here so the formatter, the tests and the documentation
cannot drift apart.
"""

from contextlib import contextmanager
from contextvars import ContextVar

# The LLD-mandated field set, in the order it is emitted.
LOG_FIELDS = (
    "correlation_id",
    "tenant_id",
    "agent_id",
    "provider_id",
    "tool_id",
    "runtime_id",
    "status",
    "duration_ms",
    "region",
)

_fields_var: ContextVar[dict] = ContextVar("log_fields", default={})


def current_fields() -> dict:
    """The fields bound for the current task, as a plain dict."""
    return dict(_fields_var.get())


def bind(**fields) -> object:
    """Merge `fields` into the current context. Returns a reset token.

    Values of None are dropped rather than stored, so a caller may pass an
    optional id unconditionally.
    """
    merged = dict(_fields_var.get())
    for key, value in fields.items():
        if key not in LOG_FIELDS:
            raise ValueError(f"unknown log field {key!r}; add it to LOG_FIELDS deliberately")
        if value is not None:
            merged[key] = value
    return _fields_var.set(merged)


def reset(token) -> None:
    _fields_var.reset(token)


def clear() -> object:
    return _fields_var.set({})


@contextmanager
def bound(**fields):
    """Bind fields for the duration of a block, then restore."""
    token = bind(**fields)
    try:
        yield
    finally:
        reset(token)
