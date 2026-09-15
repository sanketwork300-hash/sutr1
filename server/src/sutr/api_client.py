"""HTTP execution client for CustomIntegration tools.

Handles both ApiTool (declarative REST) and CustomTool (code-backed)
tools, returning results in the same format as the MCP client.
"""

import json
import time

import httpx
from sqlmodel import Session

from sutr.db import engine
from sutr.integrations.types import ApiTool, CustomIntegration, CustomTool, Param, TokenAuth
from sutr.models.integration import InstalledIntegration
from sutr.models.oauth import OAuthState
from sutr.observability.metrics import observe_provider_request
from sutr.observability.propagation import outbound_headers
from sutr.observability.tracing import span
from sutr.resilience import breaker, retry
from sutr.resilience.timeouts import provider_timeout
from sutr.runtime import request_builder
from sutr.secrets.records import get_secret_value
from sutr.token_auth import build_token_auth_headers
from sutr.upstream_safety import UnsafeUpstreamUrlError, validate_safe_url

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _auth_headers(
    installed: InstalledIntegration,
    oauth_state: OAuthState | None = None,
    integration: CustomIntegration | None = None,
) -> dict[str, str]:
    if installed.auth_method == "token":
        with Session(engine) as session:
            token = get_secret_value(session, installed.token_secret_id)
        if token:
            token_auth = _token_auth(integration)
            if token_auth:
                return build_token_auth_headers(token_auth.header, token_auth.format, token)
            return build_token_auth_headers("Authorization", "Bearer {token}", token)

    if installed.auth_method == "oauth" and oauth_state:
        with Session(engine) as session:
            access_token = get_secret_value(session, oauth_state.access_token_secret_id)
        if access_token:
            return {"Authorization": f"Bearer {access_token}"}

    return {}


def _token_auth(integration: CustomIntegration | None) -> TokenAuth | None:
    if integration is None:
        return None
    for auth in integration.auth:
        if isinstance(auth, TokenAuth):
            return auth
    return None


# ---------------------------------------------------------------------------
# Param → JSON Schema
# ---------------------------------------------------------------------------


def params_to_input_schema(params: list[Param]) -> dict:
    """Convert a list of Param definitions to a JSON Schema object.

    Delegates to `runtime.request_builder`, which is the same code the
    generated standalone packages run — the schema an agent sees from the
    gateway and from a deployed server are the same object by construction
    (ADR-010).
    """
    return request_builder.input_schema([p.model_dump() for p in params])


# ---------------------------------------------------------------------------
# URL / request construction
#
# All of it lives in `runtime.request_builder`, which the generated standalone
# packages embed verbatim (ADR-010). Nothing about how a request is built may
# be re-implemented here.
# ---------------------------------------------------------------------------


def _extract_path_params(path: str) -> list[str]:
    return request_builder.path_param_names(path)


# Thin delegations kept so the long-standing unit tests keep exercising the
# gateway's contract directly. They must stay one-liners: any logic added here
# is logic the generated standalone runtime would not have.


def _build_url(base_url: str, path: str, args: dict) -> str:
    return request_builder.build_url(base_url, path, args)


def _build_query(tool_def: ApiTool, args: dict) -> dict:
    return request_builder.build_query([p.model_dump() for p in tool_def.params], args)


def _build_param_headers(tool_def: ApiTool, args: dict) -> dict[str, str]:
    return request_builder.build_param_headers([p.model_dump() for p in tool_def.params], args)


def _build_body(tool_def: ApiTool, args: dict):
    body = request_builder.build_body(tool_def.model_dump(), args)
    return body["json"] if body["kind"] == request_builder.JSON else None


async def _read_response_body(
    response: httpx.Response, max_response_bytes: int | None = None
) -> tuple[bytes, bool]:
    if max_response_bytes is None:
        return await response.aread(), False

    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        if total + len(chunk) > max_response_bytes:
            keep = max_response_bytes - total
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), truncated


def _decode_response_body(response: httpx.Response, body: bytes) -> str:
    if not body:
        return ""
    encoding = response.encoding or "utf-8"
    return body.decode(encoding, errors="replace")


def _result_from_response(
    response: httpx.Response,
    body: bytes,
    duration_ms: int,
    *,
    truncated: bool,
) -> dict:
    text = _decode_response_body(response, body)
    if truncated:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "API response exceeded the maximum allowed response size.",
                }
            ],
            "isError": True,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }

    if response.status_code >= 400:
        error_text = text
        try:
            error_text = json.dumps(json.loads(text))
        except Exception:
            pass
        return {
            "content": [
                {"type": "text", "text": f"API error ({response.status_code}): {error_text}"}
            ],
            "isError": True,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }

    try:
        result = json.loads(text)
        content_text = json.dumps(result, indent=2)
    except Exception:
        content_text = text or "(empty response)"

    return {
        "content": [{"type": "text", "text": content_text}],
        "isError": False,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
    }


def _timeout_policy(timeout) -> httpx.Timeout:
    """Honour a caller's explicit policy; give a bare number the full one.

    Callers that pass a float mean "this many seconds overall", and they should
    still get separate connect, read and pool limits underneath it.
    """
    if isinstance(timeout, httpx.Timeout):
        return timeout
    return provider_timeout(float(timeout))


def _request_body_size(body: dict) -> int:
    """Approximate encoded size of a request body, for the size cap."""
    kind = body["kind"]
    if kind == request_builder.NONE:
        return 0
    if kind == request_builder.JSON:
        return len(json.dumps(body["json"], separators=(",", ":")).encode())
    if kind == request_builder.FORM:
        return sum(len(str(k)) + len(str(v)) + 2 for k, v in body["data"].items())
    if kind == request_builder.MULTIPART:
        fields = sum(len(str(k)) + len(str(v)) + 2 for k, v in body["data"].items())
        return fields + sum(len(part[1]) for part in body["files"].values())
    content = body["content"]
    return len(content if isinstance(content, bytes) else content.encode())


async def dispatch_api_tool(
    *,
    base_url: str,
    tool_def: ApiTool,
    args: dict,
    headers: dict[str, str],
    timeout: float | httpx.Timeout = 30.0,
    follow_redirects: bool = False,
    max_request_body_bytes: int | None = None,
    max_response_bytes: int | None = None,
    credentials: list[dict] | None = None,
) -> dict:
    """Execute a declarative ApiTool against an HTTP API.

    The request itself is constructed by `runtime.request_builder` — the same
    module a generated standalone package runs — so the hosted gateway and a
    deployed server send identical bytes for identical arguments (ADR-010).
    """
    try:
        request = request_builder.build_request(
            base_url,
            tool_def.model_dump(),
            args,
            auth_headers=headers,
            credentials=credentials,
        )
    except request_builder.RequestBuildError as exc:
        return {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
            "status_code": None,
            "duration_ms": 0,
        }

    # Re-validate the full URL at dispatch time. The base URL was checked when
    # the integration was saved, but the hostname's DNS records may have
    # changed (or been crafted) to point at internal IPs since then.
    try:
        validate_safe_url(request["url"], allow_query=True)
    except UnsafeUpstreamUrlError as exc:
        return {
            "content": [{"type": "text", "text": f"Unsafe upstream URL: {exc}"}],
            "isError": True,
            "status_code": None,
            "duration_ms": 0,
        }

    if max_request_body_bytes is not None:
        size = _request_body_size(request["body"])
        if size > max_request_body_bytes:
            return {
                "content": [{"type": "text", "text": "API request body is too large."}],
                "isError": True,
                "status_code": None,
                "duration_ms": 0,
            }

    # The Provider end of the LLD §5.3 trace. Trace context rides on the
    # request so a provider that speaks W3C trace context can parent its own
    # spans onto this call; a provider that does not simply ignores two
    # headers. The correlation id goes with it either way, which is what makes
    # a provider's support ticket findable in this platform's logs.
    request["headers"] = {**request["headers"], **outbound_headers()}

    # The circuit is keyed by host rather than by integration: two integrations
    # pointing at the same upstream share its fate, and a host that is down is
    # down for both.
    host = httpx.URL(request["url"]).host
    #
    # Raised rather than returned as an error result, deliberately: a call the
    # breaker refused never happened, and returning a result would record it as
    # an execution — metered, logged as `executed`, and counted against a
    # provider that was never asked anything. The pipeline turns the exception
    # into an error outcome, which is what it was.
    breaker.allow(host)

    start = time.perf_counter()
    # Attributes name the host and method only. The full URL is deliberately
    # absent: credentials may be carried in a query string (ADR-009), and a
    # span leaves the process.
    with span(
        "sutr.provider.request",
        kind="client",
        **{
            "sutr.stage": "provider",
            "http.request.method": request["method"],
            "server.address": httpx.URL(request["url"]).host,
        },
    ) as active_span:

        async def attempt():
            async with httpx.AsyncClient(
                timeout=_timeout_policy(timeout), follow_redirects=follow_redirects
            ) as client:
                async with client.stream(**request_builder.request_kwargs(request)) as response:
                    body, truncated = await _read_response_body(response, max_response_bytes)
                    return response, body, truncated

        try:
            # Retries apply to transport failures on safe methods only: a
            # response that arrived reached the provider's application, and
            # this platform does not know whether that tool was idempotent.
            response, body_bytes, truncated = await retry.with_retries(
                attempt, method=request["method"]
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            observe_provider_request("http", "error", duration_ms)
            if retry.is_provider_failure(exc):
                # `str(exc)` on an httpx timeout is often empty, which left an
                # operator reading `last_error: null` after five failures.
                # The class name is a poor message and a much better nothing.
                breaker.record_failure(host, str(exc) or exc.__class__.__name__)
            if active_span is not None:
                active_span.record_exception(exc)
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)
        outcome = "error" if response.status_code >= 400 else "ok"
        observe_provider_request("http", outcome, duration_ms)
        # A 5xx is the provider saying it is broken, so it counts against its
        # health. A 4xx is the provider working correctly and disagreeing with
        # the request, so it does not — opening a circuit on a run of 404s
        # would take a healthy provider out of service.
        if response.status_code >= 500:
            breaker.record_failure(host, f"HTTP {response.status_code}")
        else:
            breaker.record_success(host)
        if active_span is not None:
            active_span.set_attribute("http.response.status_code", response.status_code)
            active_span.set_attribute("sutr.duration_ms", duration_ms)

    return _result_from_response(
        response,
        body_bytes,
        duration_ms,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Public API — mirrors mcp/client.py interface
# ---------------------------------------------------------------------------


def list_tools(integration: CustomIntegration) -> list[dict]:
    """Return tool definitions as MCP-compatible dicts."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": params_to_input_schema(t.params),
        }
        for t in integration.tools
    ]


def get_tool_def(integration: CustomIntegration, tool_name: str) -> ApiTool | CustomTool | None:
    """Look up a single tool definition by name."""
    for t in integration.tools:
        if t.name == tool_name:
            return t
    return None


async def call_tool(
    installed: InstalledIntegration,
    tool_def: ApiTool | CustomTool,
    args: dict,
    oauth_state: OAuthState | None = None,
    integration: CustomIntegration | None = None,
    auth_headers: dict[str, str] | None = None,
    credentials: list[dict] | None = None,
) -> dict:
    """Execute a tool call and return an MCP-compatible result dict.

    `credentials` carries per-scheme credentials for compiled API integrations
    (ADR-009): API keys in query strings or cookies, several schemes at once,
    and OAuth2 client-credentials tokens the platform obtained itself. It is
    resolved by the caller so this function stays free of database access.
    """
    headers = (
        auth_headers
        if auth_headers is not None
        else _auth_headers(installed, oauth_state, integration)
    )

    if isinstance(tool_def, CustomTool):
        return await tool_def.run(args, headers)

    return await dispatch_api_tool(
        base_url=installed.url,
        tool_def=tool_def,
        args=args,
        headers=headers,
        credentials=credentials,
    )
