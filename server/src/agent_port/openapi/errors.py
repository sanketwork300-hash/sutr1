"""Errors and warning records for the OpenAPI import/compile pipeline."""

from pydantic import BaseModel


class OpenAPIError(ValueError):
    """A user-facing import/compile failure. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Warning_(BaseModel):
    """A non-fatal issue surfaced to the user (e.g. unsupported feature skipped)."""

    code: str
    message: str
    # Where it applies, e.g. "GET /users" or a scheme name. Optional.
    context: str | None = None
