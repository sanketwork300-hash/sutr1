import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
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

# basicConfig must run before any sutr module initializes its logger
# so the format applies everywhere — hence the intentional late imports below.
# sutr.config is safe to import first: it creates no loggers.
from sutr.config import settings  # noqa: E402

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from sutr.analytics import posthog_client  # noqa: E402
from sutr.api import (  # noqa: E402
    admin,
    api_keys,
    auth,
    billing,
    custom_api,
    custom_mcp,
    email_verification,
    google_login,
    installed,
    integrations,
    logs,
    oauth_server,
    openapi_projects,
    org_settings,
    orgs,
    password_change,
    password_reset,
    tool_approvals,
    tool_settings,
    tools,
    totp,
    user_auth,
    users,
    workspaces,
)
from sutr.api import config as config_api  # noqa: E402
from sutr.maintenance import maintenance_loop  # noqa: E402
from sutr.mcp.asgi import mcp_asgi_app  # noqa: E402
from sutr.mcp.oauth_provider import oauth_provider  # noqa: E402
from sutr.mcp.refresh import tool_cache_refresh_loop  # noqa: E402
from sutr.mcp.server import session_manager  # noqa: E402
from sutr.request_context import (  # noqa: E402
    get_request_id,
    new_request_id,
    set_request_id,
)

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
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
    tasks = [
        asyncio.create_task(tool_cache_refresh_loop()),
        asyncio.create_task(maintenance_loop()),
    ]
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

        inbound = next(
            (value for name, value in scope.get("headers", []) if name == b"x-request-id"),
            None,
        )
        request_id = inbound.decode("latin-1")[:128] if inbound else new_request_id()

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Request-ID", request_id)
                for name, value in self._SECURITY_HEADERS:
                    headers.setdefault(name.decode("latin-1"), value.decode("latin-1"))
            await send(message)

        # Deliberately no reset: the catch-all exception handler runs in
        # Starlette's outermost ServerErrorMiddleware, i.e. after this frame
        # unwinds, and still needs the ID. Every request sets a fresh value,
        # so nothing stale can leak into another request.
        set_request_id(request_id)
        await self.app(scope, receive, send_with_headers)


app.add_middleware(_RequestContextMiddleware)


class _MCPPathMiddleware:
    """Normalise /mcp (no trailing slash) → /mcp/ before routing.

    Starlette's Mount("/mcp") compiles to regex ^/mcp/(?P<path>.*)$ so a
    request for exactly /mcp never matches.  FastAPI's redirect_slashes=True
    then issues a 307 to /mcp/, but HTTP clients (including Claude Code) drop
    the Authorization header when following that redirect, causing a 401.
    Normalising the path here prevents the redirect entirely.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/"}
        await self.app(scope, receive, send)


app.add_middleware(_MCPPathMiddleware)


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
app.include_router(openapi_projects.router)
app.include_router(org_settings.router)
app.include_router(orgs.router)
app.include_router(orgs.accept_router)
app.include_router(workspaces.router)
app.include_router(tool_approvals.router)
app.include_router(logs.router)
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


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
