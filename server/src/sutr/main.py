import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler as _default_http_handler
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

# Logging must be configured before any sutr module initializes its logger so
# the format applies everywhere — hence the intentional late imports below.
# These two are safe to import first: neither creates a logger.
from sutr.config import settings  # noqa: E402
from sutr.observability import log_context  # noqa: E402
from sutr.observability.log_setup import configure_logging  # noqa: E402

configure_logging(
    level=settings.log_level,
    log_format=settings.log_format,
    service=settings.otel_service_name,
)
logger = logging.getLogger(__name__)

from sqlmodel import Session  # noqa: E402

from sutr.analytics import posthog_client  # noqa: E402
from sutr.api import (  # noqa: E402
    admin,
    api_keys,
    audit,
    auth,
    billing,
    connections,
    custom_api,
    custom_mcp,
    deployments,
    email_verification,
    google_login,
    installed,
    integration_credentials,
    integrations,
    logs,
    marketplace,
    oauth_server,
    openapi_lint,
    openapi_projects,
    org_settings,
    orgs,
    password_change,
    password_reset,
    quotas,
    tool_approvals,
    tool_settings,
    tools,
    totp,
    usage,
    user_auth,
    users,
    workspaces,
)
from sutr.api import config as config_api  # noqa: E402
from sutr.api import health as health_api  # noqa: E402
from sutr.api.v1 import billing as v1_billing  # noqa: E402
from sutr.api.v1 import discovery as v1_discovery  # noqa: E402
from sutr.api.v1 import documentation as v1_documentation  # noqa: E402
from sutr.api.v1 import events as v1_events  # noqa: E402
from sutr.api.v1 import generation as v1_generation  # noqa: E402
from sutr.api.v1 import governance as v1_governance  # noqa: E402
from sutr.api.v1 import marketplace as v1_marketplace  # noqa: E402
from sutr.api.v1 import observability as v1_observability  # noqa: E402
from sutr.api.v1 import platform as v1_platform  # noqa: E402
from sutr.api.v1 import provisioning as v1_provisioning  # noqa: E402
from sutr.api.v1 import registry as v1_registry  # noqa: E402
from sutr.api.v1 import resilience as v1_resilience  # noqa: E402
from sutr.api.v1 import sources as v1_sources  # noqa: E402
from sutr.common.errors import PlatformError, error_body  # noqa: E402
from sutr.db import get_session  # noqa: E402
from sutr.discovery import cache as discovery_cache  # noqa: E402
from sutr.events.relay import relay_loop  # noqa: E402
from sutr.maintenance import deployment_monitor_loop, maintenance_loop  # noqa: E402
from sutr.marketplace import projection as marketplace_projection  # noqa: E402
from sutr.mcp.asgi import mcp_asgi_app  # noqa: E402
from sutr.mcp.oauth_provider import oauth_provider  # noqa: E402
from sutr.mcp.refresh import tool_cache_refresh_loop  # noqa: E402
from sutr.mcp.server import session_manager  # noqa: E402
from sutr.mcp.sse import messages_app, sse_app  # noqa: E402
from sutr.observability.collectors import sample as sample_gauges  # noqa: E402
from sutr.observability.metrics import observe_http_request  # noqa: E402
from sutr.observability.metrics import render as render_metrics  # noqa: E402
from sutr.observability.propagation import extract as extract_trace_context  # noqa: E402
from sutr.observability.tracing import configure_tracing, span, tracing_enabled  # noqa: E402
from sutr.platform import leadership, mode  # noqa: E402
from sutr.request_context import (  # noqa: E402
    get_correlation_id,
    get_request_id,
    new_request_id,
    set_correlation_id,
    set_request_id,
)
from sutr.services.source_sync_loop import source_sync_loop  # noqa: E402

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"

# How long a caller should wait before retrying a write against a standby.
# Promotion is a human or an orchestrator's decision, not something that
# happens in seconds, so this is a "come back later", not a "try again now".
RETRY_AFTER_STANDBY_SECONDS = 60
HTML_CACHE_CONTROL = "no-cache"


def validate_startup_settings() -> None:
    if settings.dev:
        return
    if settings.jwt_secret_key == "change-me-in-production":
        raise RuntimeError(
            "Refusing to start with the default JWT secret key. Set JWT_SECRET_KEY to a "
            "secure value before starting Sutr."
        )


def check_database_schema() -> None:
    """Fail loudly at boot when the database is behind the migration head.

    Migrations run outside the app (compose command / fly release_command), so a
    bare `uvicorn` start against an unmigrated database previously surfaced only
    as opaque per-query errors. In dev this downgrades to a warning so a fresh
    checkout can still boot for exploration.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    from sutr import db as db_module

    server_root = Path(__file__).resolve().parent.parent.parent
    ini_path = server_root / "alembic.ini"
    if not ini_path.exists():
        logger.warning("alembic.ini not found at %s; skipping schema version check", ini_path)
        return

    script = ScriptDirectory.from_config(AlembicConfig(str(ini_path)))
    head = script.get_current_head()

    with db_module.engine.connect() as conn:
        if inspect(conn).has_table("alembic_version"):
            current = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        else:
            current = None

    if current != head:
        message = (
            f"Database schema is at revision {current!r} but this build expects {head!r}. "
            "Run 'uv run alembic upgrade head' from server/ before starting."
        )
        if settings.dev:
            logger.warning(message)
        else:
            raise RuntimeError(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_startup_settings()
    check_database_schema()
    configure_tracing()
    # The marketplace is a projection of registry events, so its handlers have
    # to be registered before the relay starts delivering any (LLD §3.7), and
    # the discovery cache invalidates off the same events (LLD §3.8).
    marketplace_projection.install()
    discovery_cache.install()
    # The tool cache refresh is per-process work — it warms *this* replica's
    # cache — so every replica runs it.
    tasks = [asyncio.create_task(tool_cache_refresh_loop())]

    # Everything else must happen exactly once across the deployment, so each
    # runs under a lease (ADR-074). Two replicas sweeping deployments would
    # meter the same five minutes twice and bill a tenant for ten.
    #
    # A standby runs none of them: they all write, and its database will not
    # accept writes.
    if mode.writes_allowed():
        singletons = [
            (leadership.JOB_MAINTENANCE, maintenance_loop),
            (leadership.JOB_DEPLOYMENT_MONITOR, deployment_monitor_loop),
        ]
        if settings.source_sync_enabled:
            singletons.append((leadership.JOB_SOURCE_SYNC, source_sync_loop))
        if settings.event_relay_enabled:
            # The lease is per job, so a standalone `sutr-event-relay` worker
            # and the API replicas contend for this one lease and only one of
            # them relays — while the API leader keeps sweeping under its own.
            singletons.append((leadership.JOB_EVENT_RELAY, relay_loop))
        tasks.extend(
            asyncio.create_task(leadership.run_as_singleton(job, factory))
            for job, factory in singletons
        )
    else:
        logger.info("standby mode: serving reads, refusing writes, running no background jobs")
    try:
        async with session_manager.run():
            yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        posthog_client.flush()


def _ui_file_response(path: Path, *, cache_control: str | None = None) -> FileResponse:
    headers = {"Cache-Control": cache_control} if cache_control else None
    return FileResponse(path, headers=headers)


app = FastAPI(
    title="Sutr",
    description="Universal tool gateway for AI agents",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code >= 500:
        logger.error(
            "HTTP %d on %s %s [request_id=%s]: %s",
            exc.status_code,
            request.method,
            request.url.path,
            get_request_id(),
            exc.detail,
        )
    return await _default_http_handler(request, exc)


@app.exception_handler(PlatformError)
async def _platform_error_handler(request: Request, exc: PlatformError):
    """Render a service-layer error in the standard shape (ADR-021).

    A service raises `PlatformError` without knowing it is being called over
    HTTP; this is the one place that decides the status code and the body, so
    the same failure looks the same on every route.
    """
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return JSONResponse(
        status_code=exc.status,
        content=exc.body(request_id=get_request_id(), correlation_id=get_correlation_id()),
        headers=headers,
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    request_id = get_request_id()
    logger.exception(
        "Unhandled exception on %s %s [request_id=%s]",
        request.method,
        request.url.path,
        request_id,
    )
    # Keep the FastAPI-default "detail" shape for client compatibility; the
    # request_id lets a user report an error we can find in the logs.
    # This response is sent by the outermost ServerErrorMiddleware, outside the
    # header-injecting middleware, so the request ID header is set here directly.
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": request_id},
        headers={"X-Request-ID": request_id} if request_id else None,
    )


app.add_middleware(GZipMiddleware, minimum_size=500)

app.mount("/mcp", mcp_asgi_app)
# The SSE transport lives at the paths the MCP specification names for it, and
# outside the /mcp mount so the two transports never contend for a path.
app.mount("/sse", sse_app)
app.mount("/messages", messages_app)


class _RequestContextMiddleware:
    """Assign a request ID and add baseline security headers.

    Pure ASGI (not BaseHTTPMiddleware) so it never interferes with the /mcp
    mount's SSE streaming. An inbound X-Request-ID (e.g. proxy-assigned) is
    honoured; otherwise one is minted. The ID is echoed on the response and
    exposed to handlers via sutr.request_context.
    """

    _SECURITY_HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_in = scope.get("headers", [])
        inbound = next((value for name, value in headers_in if name == b"x-request-id"), None)
        request_id = inbound.decode("latin-1")[:128] if inbound else new_request_id()
        # A caller-supplied correlation id keeps one logical operation joined
        # across hops; without one, the request id serves as its own.
        inbound_correlation = next(
            (value for name, value in headers_in if name == b"x-correlation-id"), None
        )
        correlation_id = (
            inbound_correlation.decode("latin-1")[:128] if inbound_correlation else request_id
        )

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Request-ID", request_id)
                headers.setdefault("X-Correlation-ID", correlation_id)
                for name, value in self._SECURITY_HEADERS:
                    headers.setdefault(name.decode("latin-1"), value.decode("latin-1"))
            await send(message)

        # Deliberately no reset: the catch-all exception handler runs in
        # Starlette's outermost ServerErrorMiddleware, i.e. after this frame
        # unwinds, and still needs the ID. Every request sets a fresh value,
        # so nothing stale can leak into another request.
        set_request_id(request_id)
        set_correlation_id(correlation_id)
        # Start every request from a clean field set: the pipeline enriches it
        # with tenant/provider/tool ids as they become known, and a value left
        # over from a previous request would mislabel this one's log lines.
        log_context.clear()
        log_context.bind(correlation_id=correlation_id)
        await self.app(scope, receive, send_with_headers)


class _TracingMiddleware:
    """The Gateway end of the trace the LLD asks for (§5.3).

    Every request opens one server span. When the caller sent W3C trace context
    the span continues *their* trace rather than starting a new one, which is
    what makes a trace span Gateway → Discovery → Runtime → Provider instead of
    breaking into one disconnected trace per hop.

    Added before the request-context middleware so it runs *inside* it: the
    span records the correlation id, and that id has to exist first.

    Skipped for the scrape and health endpoints — a span per scrape is noise
    that costs money in a trace backend and tells nobody anything — and for the
    SSE stream, which is one request that stays open for as long as a client is
    connected. A span around it would be exported only on disconnection, hours
    later, describing nothing that happened inside. The JSON-RPC posts to
    /messages are traced normally.
    """

    SKIP_PATHS = ("/metrics", "/health", "/health/live", "/health/ready")
    SKIP_PREFIXES = ("/sse",)

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in self.SKIP_PATHS
            or path.startswith(self.SKIP_PREFIXES)
            or not tracing_enabled()
        ):
            await self.app(scope, receive, send)
            return

        headers = {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        remote_context = extract_trace_context(headers)
        status_code = 500

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        with span(
            "sutr.gateway",
            context=remote_context,
            kind="server",
            **{
                "http.request.method": scope.get("method", "GET"),
                "url.path": scope.get("path", ""),
                "sutr.stage": "gateway",
            },
        ) as active_span:
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                if active_span is not None:
                    active_span.set_attribute("http.response.status_code", status_code)
                    # The route template, resolved by the router below us. It
                    # is the low-cardinality name of what was called; the raw
                    # path is already on the span for the one case where the
                    # ids matter.
                    route = scope.get("route")
                    template = getattr(route, "path_format", None) or getattr(route, "path", None)
                    if template:
                        active_span.set_attribute("http.route", template)


app.add_middleware(_TracingMiddleware)


app.add_middleware(_RequestContextMiddleware)


class _MCPPathMiddleware:
    """Normalise a mounted transport path (no trailing slash) → with one.

    Starlette's Mount("/mcp") compiles to regex ^/mcp/(?P<path>.*)$ so a
    request for exactly /mcp never matches.  FastAPI's redirect_slashes=True
    then issues a 307 to /mcp/, but HTTP clients (including Claude Code) drop
    the Authorization header when following that redirect, causing a 401.
    Normalising the path here prevents the redirect entirely.

    The same applies to the SSE transport's two paths, which are mounted the
    same way and are reached by the same credential-carrying clients.
    """

    MOUNTS = ("/mcp", "/sse", "/messages")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self.MOUNTS:
            scope = {**scope, "path": scope["path"] + "/"}
        await self.app(scope, receive, send)


app.add_middleware(_MCPPathMiddleware)


class _MetricsMiddleware:
    """Record request counts and latency, labelled by matched route template.

    Runs inside the router, so after `self.app(...)` returns, Starlette has
    merged the matched route into the (mutable) scope. Unmatched paths collapse
    to "other" and the SSE mount to "/mcp" — never raw paths, which would turn
    ids into unbounded label values.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _route_label(scope: Scope) -> str:
        if scope.get("path", "").startswith("/mcp"):
            return "/mcp"
        route = scope.get("route")
        path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
        return path_format or "other"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            observe_http_request(
                scope.get("method", "GET"),
                self._route_label(scope),
                status_code,
                time.perf_counter() - started,
            )


app.add_middleware(_MetricsMiddleware)


class _ReadOnlyMiddleware:
    """Refuse writes on a standby instance, with a reason (LLD §5.4, §5.7).

    A standby's database is a physical replica, so a write reaches the driver
    as *"cannot execute INSERT in a read-only transaction"* and reaches the
    agent as a 500 — an error it cannot act on and will retry against the same
    instance. This turns it into a 503 that names the mode, names where writes
    are served, and carries `Retry-After`, which is an answer.

    Outermost of the application's middleware, because a request that is not
    going to be served should not be routed, traced or metered as though it
    were. In active mode — every ordinary deployment — this is one comparison
    against a string.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or mode.allows(
            scope.get("method", "GET"), scope.get("path", "")
        ):
            await self.app(scope, receive, send)
            return

        body = error_body(
            code="unavailable",
            message=mode.refusal_message(),
            details={"mode": mode.current(), "region": settings.region or None},
            retry_after=RETRY_AFTER_STANDBY_SECONDS,
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
        )
        response = JSONResponse(
            status_code=503,
            content=body,
            headers={"Retry-After": str(RETRY_AFTER_STANDBY_SECONDS)},
        )
        await response(scope, receive, send)


app.add_middleware(_ReadOnlyMiddleware)


class _ImmutableAssetStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            response.headers.setdefault("Cache-Control", ASSET_CACHE_CONTROL)
        return response


# custom_mcp must come first so its /api/integrations/custom routes win against
# the catalog's GET /api/integrations/{integration_id} catch-all.
app.include_router(custom_mcp.router)
app.include_router(custom_api.router)
app.include_router(integrations.router)
app.include_router(marketplace.router)
app.include_router(integration_credentials.router)
app.include_router(admin.router)
app.include_router(api_keys.router)
app.include_router(billing.router)
app.include_router(config_api.router)
app.include_router(users.router)
app.include_router(user_auth.router)
app.include_router(google_login.router)
app.include_router(email_verification.router)
app.include_router(password_change.router)
app.include_router(password_reset.router)
app.include_router(totp.router)
app.include_router(installed.router)
app.include_router(auth.router)
app.include_router(tools.router)
app.include_router(tool_settings.router)
app.include_router(connections.router)
app.include_router(openapi_lint.router)
app.include_router(openapi_projects.router)
app.include_router(deployments.router)
app.include_router(org_settings.router)
app.include_router(orgs.router)
app.include_router(orgs.accept_router)
app.include_router(workspaces.router)
app.include_router(tool_approvals.router)
app.include_router(logs.router)
app.include_router(audit.router)
app.include_router(usage.router)
app.include_router(quotas.router)

# ── /v1: the surface the ESDS API standards apply to (ADR-004) ──────────────
app.include_router(health_api.router)
app.include_router(v1_platform.router)
app.include_router(v1_observability.router)
app.include_router(v1_resilience.router)
app.include_router(v1_events.router)
app.include_router(v1_sources.router)
app.include_router(v1_documentation.router)
app.include_router(v1_generation.router)
app.include_router(v1_registry.router)
app.include_router(v1_marketplace.router)
app.include_router(v1_discovery.router)
app.include_router(v1_provisioning.router)
app.include_router(v1_governance.router)
app.include_router(v1_billing.metering_router)
app.include_router(v1_billing.billing_router)
app.include_router(v1_billing.settlements_router)
app.include_router(oauth_server.router)

# MCP SDK OAuth Authorization Server routes
for route in create_auth_routes(
    provider=oauth_provider,
    issuer_url=AnyHttpUrl(settings.base_url),
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
):
    app.router.routes.append(route)

# MCP SDK Protected Resource Metadata routes
for route in create_protected_resource_routes(
    resource_url=AnyHttpUrl(f"{settings.base_url}/mcp"),
    authorization_servers=[AnyHttpUrl(settings.base_url)],
    resource_name="Sutr MCP",
):
    app.router.routes.append(route)


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request, session: Session = Depends(get_session)) -> Response:
    """Prometheus scrape endpoint.

    Disabled by default (404, so its existence isn't advertised). When
    METRICS_ENABLED=true and METRICS_TOKEN is set, a matching bearer token is
    required. Series carry no tenant labels — see observability/metrics.py.

    Queue-depth and lag gauges are sampled here, on the scrape, so the value a
    scrape returns is the value that was true when it answered.
    """
    if not settings.metrics_enabled:
        raise StarletteHTTPException(status_code=404, detail="Not Found")
    if settings.metrics_token:
        header = request.headers.get("authorization", "")
        presented = header[7:] if header.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(presented, settings.metrics_token):
            raise StarletteHTTPException(status_code=401, detail="Unauthorized")
    sample_gauges(session)
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


# ─── UI static file serving ───────────────────────────────────────────────────
# ui_dist/ is copied in by the Dockerfile. Skip silently in local dev where
# only the Python server runs (UI is served by Vite on :5173).

_ui_dist = Path(__file__).parent.parent.parent / "ui_dist"

if _ui_dist.exists():
    _ui_index = _ui_dist / "index.html"

    # Serve hashed JS/CSS bundles from /assets (Vite's default output dir).
    app.mount(
        "/assets",
        _ImmutableAssetStaticFiles(directory=_ui_dist / "assets"),
        name="ui-assets",
    )

    # Catch-all: serve any existing root-level static file (favicon, etc.),
    # otherwise hand off to the React app so client-side routing works.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        candidate = (_ui_dist / full_path).resolve()
        if candidate.is_relative_to(_ui_dist) and candidate.is_file():
            cache_control = HTML_CACHE_CONTROL if candidate == _ui_index else None
            return _ui_file_response(candidate, cache_control=cache_control)
        return _ui_file_response(_ui_index, cache_control=HTML_CACHE_CONTROL)
