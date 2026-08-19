"""Exception hierarchy.

Every error carries the server's `X-Request-ID` when present, so a user can
quote it in a bug report and the operator can find the matching log line.
"""


class SutrError(Exception):
    """Base class for every error raised by this SDK."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id

    def __str__(self) -> str:
        if self.request_id:
            return f"{self.message} (request_id={self.request_id})"
        return self.message


class SutrConnectionError(SutrError):
    """The server could not be reached at all."""


class APIError(SutrError):
    """An HTTP error that has no more specific subclass."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        body: object = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.status_code = status_code
        self.body = body


class AuthenticationError(APIError):
    """401 — the API key or token is missing, wrong, or revoked."""


class PermissionDenied(APIError):
    """403 — authenticated, but the caller's role forbids this action."""


class NotFoundError(APIError):
    """404 — no such integration, tool, project, or deployment."""


class RateLimited(APIError):
    """429 — slow down. `retry_after` is seconds, when the server said so."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ServerError(APIError):
    """5xx — the server failed to handle an otherwise valid request."""


class ToolDenied(SutrError):
    """The tool's policy is `deny`: it can never run until a human changes it.

    Not retryable — waiting will not help.
    """

    def __init__(
        self,
        message: str,
        *,
        integration_id: str | None = None,
        tool_name: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.integration_id = integration_id
        self.tool_name = tool_name


class ApprovalRequired(SutrError):
    """The call is gated on a human decision.

    Share `approval_url` with a human, then either wait for the decision
    (`client.await_approval(err.request_id_for_approval)`) or re-issue the call
    with `wait_for_approval=True`.
    """

    def __init__(
        self,
        message: str,
        *,
        approval_url: str,
        approval_request_id: str,
        integration_id: str | None = None,
        tool_name: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.approval_url = approval_url
        # Named to avoid colliding with the HTTP `request_id` above.
        self.approval_request_id = approval_request_id
        self.integration_id = integration_id
        self.tool_name = tool_name


class ApprovalPending(SutrError):
    """Waiting timed out client-side without a decision. Try again later."""

    def __init__(
        self,
        message: str,
        *,
        approval_url: str | None = None,
        approval_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.approval_url = approval_url
        self.approval_request_id = approval_request_id
