"""Per-request context shared across middleware, handlers, and logging.

The request ID is assigned by the ASGI middleware in main.py and read by the
exception handlers so a 500 response and its log line can be correlated.
"""

import uuid
from contextvars import ContextVar

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(request_id: str):
    return _request_id_var.set(request_id)


def reset_request_id(token) -> None:
    _request_id_var.reset(token)


def get_request_id() -> str | None:
    return _request_id_var.get()
