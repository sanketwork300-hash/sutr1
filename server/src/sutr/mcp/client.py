import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlmodel import Session

from sutr.db import engine
from sutr.integrations.registry import CUSTOM_PREFIX
from sutr.models.integration import InstalledIntegration
from sutr.models.oauth import OAuthState
from sutr.observability.metrics import observe_provider_request
from sutr.observability.propagation import outbound_headers
from sutr.observability.tracing import span
from sutr.resilience import breaker
from sutr.secrets.records import get_secret_value


def _auth_headers(installed: InstalledIntegration, oauth_state: OAuthState | None = None) -> dict:
    if installed.auth_method == "token":
        with Session(engine) as session:
            token = get_secret_value(session, installed.token_secret_id)
        if token:
            return {"Authorization": f"Bearer {token}"}

    if installed.auth_method == "oauth" and oauth_state:
        with Session(engine) as session:
            access_token = get_secret_value(session, oauth_state.access_token_secret_id)
        if access_token:
            return {"Authorization": f"Bearer {access_token}"}

    return {}


def _ensure_safe_upstream(installed: InstalledIntegration) -> None:
    """Re-validate user-supplied MCP URLs at connection time (DNS can change
    after the definition was saved). Bundled integrations use vendor-constant
    URLs and skip the check."""
    if installed.integration_id.startswith(CUSTOM_PREFIX):
        from sutr.upstream_safety import validate_safe_url

        validate_safe_url(installed.url)


def _unwrap_exception(e: BaseException) -> str:
    """Return a readable error string, unpacking ExceptionGroup sub-exceptions."""
    if isinstance(e, BaseExceptionGroup):
        parts = [_unwrap_exception(sub) for sub in e.exceptions]
        return "; ".join(parts)
    return str(e)


async def validate_token(url: str, token: str) -> None:
    """Verify a token works by attempting to list tools. Raises RuntimeError on failure."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                await session.list_tools()
    except BaseExceptionGroup as eg:
        raise RuntimeError(_unwrap_exception(eg)) from eg


async def list_tools(
    installed: InstalledIntegration, oauth_state: OAuthState | None = None
) -> list[dict]:
    _ensure_safe_upstream(installed)
    headers = _auth_headers(installed, oauth_state)
    try:
        async with streamablehttp_client(installed.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                return [t.model_dump() for t in result.tools]
    except BaseExceptionGroup as eg:
        raise RuntimeError(_unwrap_exception(eg)) from eg


async def call_tool(
    installed: InstalledIntegration,
    tool_name: str,
    args: dict,
    oauth_state: OAuthState | None = None,
) -> dict:
    _ensure_safe_upstream(installed)
    # Trace context and the correlation id ride to the upstream MCP server for
    # the same reason they ride to an HTTP provider: so the far end's spans and
    # logs can be joined to this call (LLD §5.3).
    headers = outbound_headers(_auth_headers(installed, oauth_state))

    # The same circuit as the HTTP transport uses, keyed by host: an upstream
    # that is failing is failing whichever protocol reaches it.
    host = httpx.URL(installed.url).host
    # Raised, not returned: a call the breaker refused never happened, and a
    # returned result would be recorded as an execution.
    breaker.allow(host)

    started = time.perf_counter()
    # The host, not the URL: an upstream MCP URL is user-supplied and can carry
    # a token in its query string.
    with span(
        "sutr.provider.request",
        kind="client",
        **{
            "sutr.stage": "provider",
            "sutr.transport": "mcp",
            "sutr.tool_name": tool_name,
            "server.address": httpx.URL(installed.url).host,
        },
    ) as active_span:
        try:
            async with streamablehttp_client(installed.url, headers=headers) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, args)
        except BaseExceptionGroup as eg:
            observe_provider_request("mcp", "error", _elapsed_ms(started))
            breaker.record_failure(host, _unwrap_exception(eg))
            if active_span is not None:
                active_span.record_exception(eg)
            raise RuntimeError(_unwrap_exception(eg)) from eg
        except Exception as exc:
            observe_provider_request("mcp", "error", _elapsed_ms(started))
            breaker.record_failure(host, str(exc))
            raise

        duration_ms = _elapsed_ms(started)
        observe_provider_request("mcp", "error" if result.isError else "ok", duration_ms)
        # `isError` is the *tool* saying no, not the server failing: an upstream
        # that answers is an upstream that is up.
        breaker.record_success(host)
        if active_span is not None:
            active_span.set_attribute("sutr.duration_ms", duration_ms)
        return {
            "content": [c.model_dump() for c in result.content],
            "isError": result.isError,
        }


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
