"""MCP over Server-Sent Events (build prompt §8, Feature 8).

StreamableHTTP is the current transport and stays the default. SSE is the
transport the earlier generation of MCP clients speaks, and several still only
speak it — so the gateway offers both rather than telling those clients to
upgrade.

Two endpoints, per the MCP specification's SSE transport:

    GET  /sse       opens the event stream and announces the POST endpoint
    POST /messages  carries the client's JSON-RPC messages into that stream

The session runs in the GET request's task: that is where the MCP server loop
lives, so that is where the authenticated identity must be bound. A POST is
authenticated on its own account *and* checked against the org that opened the
session — otherwise knowing a session id would be enough to inject calls into
somebody else's stream.
"""

import logging
import uuid
from urllib.parse import parse_qs

from mcp.server.sse import SseServerTransport
from starlette.responses import Response

from sutr.config import settings
from sutr.mcp.asgi import _authenticate, _extract_header, _send_401
from sutr.mcp.server import RequestMeta, _current_auth, _current_request_meta, mcp_server

logger = logging.getLogger(__name__)

MESSAGE_PATH = "/messages"

transport = SseServerTransport(MESSAGE_PATH)

# session id (hex) → org id that opened it. Bound for the life of the stream so
# a POST can be refused when it belongs to a different tenant.
_session_owner: dict[str, uuid.UUID] = {}


def _new_session_id(before: set) -> str | None:
    """The session id `connect_sse` just created.

    The SDK generates the id inside `connect_sse` and does not hand it back —
    it only appears as a key of the transport's stream-writer registry. Taking
    it from there is the coupling that lets a POST be tenant-checked; a test
    asserts the attribute still exists so this fails loudly rather than
    silently skipping the check if the SDK changes.
    """
    added = set(transport._read_stream_writers) - before
    if len(added) != 1:
        return None
    return next(iter(added)).hex


async def handle_sse(scope, receive, send) -> None:
    """GET /sse — open the stream and run one MCP session on it."""
    auth = _authenticate(scope)
    if auth is None:
        await _send_401(send)
        return

    client = scope.get("client")
    meta = RequestMeta(
        ip=client[0] if client else None,
        user_agent=_extract_header(scope, b"user-agent"),
    )

    before = set(transport._read_stream_writers)
    session_id: str | None = None
    auth_token = _current_auth.set(auth)
    meta_token = _current_request_meta.set(meta)
    try:
        async with transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
            session_id = _new_session_id(before)
            if session_id:
                _session_owner[session_id] = auth.org.id
            else:
                logger.warning(
                    "MCP SSE: could not determine the session id; POSTs to this session "
                    "will be refused rather than accepted unchecked"
                )
            await mcp_server.run(
                read_stream, write_stream, mcp_server.create_initialization_options()
            )
    except ValueError:
        # connect_sse raises after already sending its own error response
        # (DNS-rebinding protection).
        return
    finally:
        if session_id:
            _session_owner.pop(session_id, None)
        _current_auth.reset(auth_token)
        _current_request_meta.reset(meta_token)
    # Starlette needs a response object once the stream ends.
    await Response()(scope, receive, send)


async def handle_messages(scope, receive, send) -> None:
    """POST /messages — deliver a client message into its session."""
    auth = _authenticate(scope)
    if auth is None:
        await _send_401(send)
        return

    query = parse_qs(scope.get("query_string", b"").decode())
    session_id = (query.get("session_id") or [""])[0]
    owner = _session_owner.get(session_id)
    if owner is None or owner != auth.org.id:
        # Either the session is unknown, or it belongs to another tenant.
        # The same answer for both: a caller must not be able to probe which
        # session ids exist by comparing responses.
        await _forbidden(send)
        return

    await transport.handle_post_message(scope, receive, send)


async def _forbidden(send) -> None:
    body = b'{"error":"unknown_or_foreign_session"}'
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def sse_app(scope, receive, send) -> None:
    """ASGI entry point mounted at /sse."""
    if scope["type"] != "http":
        await Response(status_code=404)(scope, receive, send)
        return
    if not settings.mcp_sse_enabled:
        await Response(status_code=404)(scope, receive, send)
        return
    if scope.get("method") != "GET":
        await Response(status_code=405, headers={"Allow": "GET"})(scope, receive, send)
        return
    await handle_sse(scope, receive, send)


async def messages_app(scope, receive, send) -> None:
    """ASGI entry point mounted at /messages."""
    if scope["type"] != "http":
        await Response(status_code=404)(scope, receive, send)
        return
    if not settings.mcp_sse_enabled:
        await Response(status_code=404)(scope, receive, send)
        return
    if scope.get("method") != "POST":
        await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
        return
    await handle_messages(scope, receive, send)
