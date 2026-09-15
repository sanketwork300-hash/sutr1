"""Per-request context shared across middleware, handlers, and logging.

The request ID is assigned by the ASGI middleware in main.py and read by the
exception handlers so a 500 response and its log line can be correlated.

The *correlation* ID is distinct (ESDS LLD §5.5, which requires both
`X-Request-ID` and `X-Correlation-ID`): a request ID identifies one HTTP
request, while a correlation ID identifies one logical operation that may span
several requests, background stages, and services. An inbound correlation ID is
honoured so a caller's trace survives the hop; when absent it defaults to the
request ID, which keeps single-request operations correlated for free.
"""

import uuid
from contextvars import ContextVar

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(request_id: str):
    return _request_id_var.set(request_id)


def reset_request_id(token) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str | None:
    return _request_id_var.get()


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(correlation_id: str):
    return _correlation_id_var.set(correlation_id)


def reset_correlation_id(token) -> None:
    _correlation_id_var.reset(token)


def get_correlation_id() -> str | None:
    return _correlation_id_var.get()
