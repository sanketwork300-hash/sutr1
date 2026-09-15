"""Errors and warning records for the OpenAPI import/compile pipeline."""

from pydantic import BaseModel


class OpenAPIError(ValueError):
    """A user-facing import/compile failure. `code` is machine-readable.

    `findings` carries the full diagnostic list when one exists (build prompt
    §16 requires every validation error, not only the first): the message names
    the first problem so a terminal still reads well, while an API response can
    render all of them.
    """

    def __init__(self, code: str, message: str, findings: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.findings = findings or []


class SpecWarning(BaseModel):
    """A non-fatal issue surfaced to the user (e.g. unsupported feature skipped)."""

    code: str
    message: str
    # Where it applies, e.g. "GET /users" or a scheme name. Optional.
    context: str | None = None
