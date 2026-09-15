"""The platform's error vocabulary and wire shape.

ESDS LLD §5.5 fixes the HTTP status contract — 400 bad request, 401
unauthorized, 403 forbidden, 404 not found, 409 conflict, 422 validation,
429 rate-limited, 500 internal, 503 unavailable — and requires a standard
error envelope. The condensed LLD renders the envelope itself as a graphic
whose field names are not legible in the extracted text, so the exact shape
below is an implementation decision, recorded as ADR-021 rather than
attributed to the LLD.

Two rules hold everywhere:

- `code` is machine-readable and stable; `message` is for a human and may be
  reworded freely. A client that branches on `message` is a client we have
  already broken, so `code` exists to stop that happening.
- An error never carries a credential, an argument value, or an internal
  stack. `details` is for structured, safe context (which field, which limit).
"""

from typing import Any

# code → HTTP status. The mapping is one-way and total: every error the
# platform raises has a status, and no handler invents its own.
ERROR_STATUS: dict[str, int] = {
    "bad_request": 400,
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "validation_failed": 422,
    "quota_exceeded": 429,
    "rate_limited": 429,
    "internal_error": 500,
    "unavailable": 503,
}

DEFAULT_STATUS = 400


class PlatformError(Exception):
    """An error with a machine-readable code and a safe, human-readable message.

    Raised by service layers; translated to a response by one exception
    handler, so a service never has to know it is being called over HTTP.
    """

    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.code, DEFAULT_STATUS)

    def body(self, *, request_id: str | None = None, correlation_id: str | None = None) -> dict:
        return error_body(
            code=self.code,
            message=self.message,
            details=self.details,
            retry_after=self.retry_after,
            request_id=request_id,
            correlation_id=correlation_id,
        )


class InvalidRequestError(PlatformError):
    code = "invalid_request"


class UnauthorizedError(PlatformError):
    code = "unauthorized"


class ForbiddenError(PlatformError):
    code = "forbidden"


class NotFoundError(PlatformError):
    code = "not_found"


class ConflictError(PlatformError):
    code = "conflict"


class RateLimitedError(PlatformError):
    code = "rate_limited"


class UnavailableError(PlatformError):
    code = "unavailable"


def error_body(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    retry_after: int | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """The wire shape of an error.

    `error` is always present and always an object, so a client can test one
    key to tell a failure from a success without branching on status codes it
    may not have anticipated.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    if retry_after is not None:
        error["retry_after"] = retry_after
    body: dict[str, Any] = {"error": error, "meta": {}}
    if request_id:
        body["meta"]["request_id"] = request_id
    if correlation_id:
        body["meta"]["correlation_id"] = correlation_id
    return body
