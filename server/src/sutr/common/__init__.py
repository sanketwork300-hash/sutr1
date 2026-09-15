"""Shared building blocks every platform service reuses.

The ESDS LLD §2.6 names `common` (logging, errors, pagination, responses) among
the shared libraries a service must reuse rather than reinvent. This is that
library: the error vocabulary, the response envelope, cursor pagination, and
idempotency.

It is deliberately thin and dependency-light — it may import `fastapi`,
`pydantic` and the standard library, and nothing else from `sutr` except
`request_context`. Anything richer belongs in a service.
"""

from sutr.common.errors import (
    ERROR_STATUS,
    ConflictError,
    ForbiddenError,
    InvalidRequestError,
    NotFoundError,
    PlatformError,
    RateLimitedError,
    UnauthorizedError,
    UnavailableError,
    error_body,
)
from sutr.common.pagination import Page, PageRequest, decode_cursor, encode_cursor
from sutr.common.responses import Envelope, envelope, meta_for_request

__all__ = [
    "ERROR_STATUS",
    "ConflictError",
    "Envelope",
    "ForbiddenError",
    "InvalidRequestError",
    "NotFoundError",
    "Page",
    "PageRequest",
    "PlatformError",
    "RateLimitedError",
    "UnauthorizedError",
    "UnavailableError",
    "decode_cursor",
    "encode_cursor",
    "envelope",
    "error_body",
    "meta_for_request",
]
