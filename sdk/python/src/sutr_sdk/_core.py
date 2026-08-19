"""Transport-agnostic request building and response interpretation.

The sync and async clients differ ONLY in how they move bytes; every decision
about URLs, headers, status codes, error mapping, and retry eligibility lives
here so the two can never drift apart.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sutr_sdk.errors import (
    APIError,
    ApprovalRequired,
    AuthenticationError,
    NotFoundError,
    PermissionDenied,
    RateLimited,
    ServerError,
    ToolDenied,
)

DEFAULT_BASE_URL = "https://app.sutr.sh"
DEFAULT_TIMEOUT = 30.0
# Long-poll waits are bounded server-side (~240s); allow headroom over that.
APPROVAL_POLL_TIMEOUT = 300.0
USER_AGENT = "sutr-sdk-python/0.1.0"
RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


@dataclass
class Prepared:
    method: str
    path: str
    json: Any | None = None
    params: dict[str, Any] | None = None
    timeout: float | None = None
    # False for anything with side effects — a retried tool call could execute twice.
    retryable: bool = False


def encode(value: str) -> str:
    return quote(str(value), safe="")


def build_headers(api_key: str | None, access_token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["X-API-Key"] = api_key
    elif access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def detail_message(body: Any, status_code: int) -> str:
    """Pull a human-readable message out of FastAPI's several error shapes."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error")
            if isinstance(message, str):
                return message
        if isinstance(detail, list):  # pydantic validation errors
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(p) for p in (item.get("loc") or [])[1:]) or "body"
                    parts.append(f"{loc}: {item.get('msg')}")
            if parts:
                return "; ".join(parts)
        message = body.get("message")
        if isinstance(message, str):
            return message
    return f"HTTP {status_code}"


def interpret(
    *,
    status_code: int,
    body: Any,
    headers: dict[str, str],
    context: dict[str, Any] | None = None,
) -> Any:
    """Return the parsed body, or raise the most specific error that fits.

    `context` carries integration_id/tool_name so tool-call errors can name the
    tool that was blocked.
    """
    request_id = headers.get("x-request-id") or headers.get("X-Request-ID")
    context = context or {}

    if 200 <= status_code < 300:
        return body

    # The governance responses come back as 403 with a machine-readable `error`.
    if status_code == 403 and isinstance(body, dict):
        error_code = body.get("error")
        if error_code == "approval_required":
            raise ApprovalRequired(
                body.get("message") or "Tool call requires human approval.",
                approval_url=body.get("approval_url") or "",
                approval_request_id=str(body.get("approval_request_id") or ""),
                integration_id=body.get("integration_id") or context.get("integration_id"),
                tool_name=body.get("tool_name") or context.get("tool_name"),
                request_id=request_id,
            )
        if error_code == "denied":
            raise ToolDenied(
                body.get("message") or "This tool is blocked by policy.",
                integration_id=body.get("integration_id") or context.get("integration_id"),
                tool_name=body.get("tool_name") or context.get("tool_name"),
                request_id=request_id,
            )

    message = detail_message(body, status_code)
    common = {"status_code": status_code, "body": body, "request_id": request_id}

    if status_code == 401:
        raise AuthenticationError(message, **common)
    if status_code == 403:
        raise PermissionDenied(message, **common)
    if status_code == 404:
        raise NotFoundError(message, **common)
    if status_code == 429:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        raise RateLimited(
            message,
            retry_after=float(retry_after) if _is_number(retry_after) else None,
            **common,
        )
    if status_code >= 500:
        raise ServerError(message, **common)
    raise APIError(message, **common)


def _is_number(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def retry_delay(attempt: int, retry_after: float | None) -> float:
    """Retry-After wins; otherwise exponential backoff (0.5s, 1s, 2s, …)."""
    if retry_after is not None:
        return min(retry_after, 30.0)
    return min(0.5 * (2**attempt), 8.0)


# Floor between approval polls. The server long-polls (holding the request open
# for up to ~240s), so a well-behaved wait makes very few requests. But a proxy
# that buffers and returns "pending" instantly would otherwise turn the wait
# into a hot loop hammering the API, so enforce a minimum spacing.
POLL_MIN_INTERVAL = 0.5


def poll_backoff(elapsed: float, remaining: float | None) -> float:
    """Seconds to wait before the next poll, given how fast the last one
    answered and how much of the caller's timeout budget is left."""
    delay = POLL_MIN_INTERVAL - elapsed
    if delay <= 0:
        return 0.0
    if remaining is not None:
        delay = min(delay, max(0.0, remaining))
    return delay


# ── Request builders (one per endpoint the SDK exposes) ──────────────────────


def list_tools_request(integration_id: str | None) -> Prepared:
    path = f"/api/tools/{encode(integration_id)}" if integration_id else "/api/tools"
    return Prepared("GET", path, retryable=True)


def call_tool_request(
    integration_id: str, tool_name: str, args: dict | None, additional_info: str | None
) -> Prepared:
    payload: dict[str, Any] = {"tool_name": tool_name, "args": args or {}}
    if additional_info:
        payload["additional_info"] = additional_info
    # Never retryable: a tool call has side effects by definition.
    return Prepared("POST", f"/api/tools/{encode(integration_id)}/call", json=payload)


def await_approval_request(approval_request_id: str, timeout_seconds: int | None) -> Prepared:
    payload = {"timeout_seconds": timeout_seconds} if timeout_seconds else None
    return Prepared(
        "POST",
        f"/api/tool-approvals/requests/{encode(approval_request_id)}/await",
        json=payload,
        timeout=APPROVAL_POLL_TIMEOUT,
        retryable=True,
    )


def approval_request_status_request(approval_request_id: str) -> Prepared:
    return Prepared(
        "GET",
        f"/api/tool-approvals/requests/{encode(approval_request_id)}",
        retryable=True,
    )


def integrations_request() -> Prepared:
    return Prepared("GET", "/api/integrations", retryable=True)


def installed_request() -> Prepared:
    return Prepared("GET", "/api/installed", retryable=True)


def logs_request(params: dict[str, Any]) -> Prepared:
    return Prepared("GET", "/api/logs", params=_clean(params), retryable=True)


def usage_summary_request(params: dict[str, Any]) -> Prepared:
    return Prepared("GET", "/api/usage/summary", params=_clean(params), retryable=True)


def usage_events_request(params: dict[str, Any]) -> Prepared:
    return Prepared("GET", "/api/usage/events", params=_clean(params), retryable=True)


def deployments_request() -> Prepared:
    return Prepared("GET", "/api/deployments", retryable=True)


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}
