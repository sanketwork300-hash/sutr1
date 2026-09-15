"""MCP transports: StreamableHTTP, SSE, and stdio (build prompt §8, Feature 8).

StreamableHTTP was the only transport. SSE and stdio are added *alongside* it —
the tests below check that the new ones authenticate and isolate tenants the
same way, and that the existing one is untouched.
"""

from unittest.mock import patch

from sutr.config import settings
from sutr.mcp import sse as sse_module
from sutr.mcp.stdio import ENV_VAR, authenticate_from_env

# ── The SDK coupling SSE tenant-checking depends on ──────────────────────────


def test_the_transport_still_exposes_its_session_registry():
    """`handle_messages` checks session ownership using the SDK's stream-writer
    registry, because `connect_sse` does not return the session id. If the SDK
    renames it, this fails here rather than silently skipping the check."""
    assert hasattr(sse_module.transport, "_read_stream_writers")
    assert isinstance(sse_module.transport._read_stream_writers, dict)


def test_the_message_endpoint_is_the_one_announced_to_clients():
    assert sse_module.transport._endpoint == sse_module.MESSAGE_PATH


# ── SSE endpoint behaviour ───────────────────────────────────────────────────


async def test_sse_requires_authentication(unauthenticated_client):
    resp = await unauthenticated_client.get("/sse")
    assert resp.status_code == 401
    assert "www-authenticate" in {k.lower() for k in resp.headers}


async def test_posting_a_message_requires_authentication(unauthenticated_client):
    resp = await unauthenticated_client.post("/messages?session_id=deadbeef", json={})
    assert resp.status_code == 401


async def test_posting_to_an_unknown_session_is_refused(agent_key_client):
    resp = await agent_key_client.post(
        "/messages?session_id=00000000000000000000000000000000", json={}
    )
    assert resp.status_code == 404


async def test_posting_to_another_tenants_session_is_refused(agent_key_client, test_org):
    """Knowing a session id must not be enough to inject into it."""
    import uuid

    foreign = uuid.uuid4()
    sse_module._session_owner["ffffffffffffffffffffffffffffffff"] = foreign
    try:
        resp = await agent_key_client.post(
            "/messages?session_id=ffffffffffffffffffffffffffffffff", json={}
        )
        assert resp.status_code == 404
    finally:
        sse_module._session_owner.pop("ffffffffffffffffffffffffffffffff", None)


async def test_a_post_from_the_owning_tenant_reaches_the_transport(agent_key_client, test_org):
    delivered = {}

    async def fake_handle_post_message(scope, receive, send):
        delivered["called"] = True
        body = b'{"ok":true}'
        await send(
            {
                "type": "http.response.start",
                "status": 202,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    sse_module._session_owner["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"] = test_org.id
    try:
        with patch.object(sse_module.transport, "handle_post_message", fake_handle_post_message):
            resp = await agent_key_client.post(
                "/messages?session_id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", json={"jsonrpc": "2.0"}
            )
        assert resp.status_code == 202
        assert delivered.get("called") is True
    finally:
        sse_module._session_owner.pop("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", None)


async def test_the_wrong_method_is_rejected_rather_than_mishandled(agent_key_client):
    assert (await agent_key_client.post("/sse", json={})).status_code == 405
    assert (await agent_key_client.get("/messages")).status_code == 405


async def test_sse_can_be_turned_off(agent_key_client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_sse_enabled", False)
    assert (await agent_key_client.get("/sse")).status_code == 404
    assert (await agent_key_client.post("/messages?session_id=x", json={})).status_code == 404


# ── StreamableHTTP is unchanged ──────────────────────────────────────────────


async def test_streamable_http_still_requires_authentication(unauthenticated_client):
    resp = await unauthenticated_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401


def test_streamable_http_is_still_stateful():
    """Stateful sessions are what make server-pushed `tools/list_changed`
    possible; adding transports must not quietly turn that off."""
    from sutr.mcp.server import session_manager

    assert session_manager.stateless is False


def test_all_three_transports_serve_the_same_server_object():
    """The point of adding transports is reach, not a second implementation:
    policy, approvals, logging and metering must apply identically."""
    from sutr.mcp import sse, stdio
    from sutr.mcp.server import mcp_server, session_manager

    assert session_manager.app is mcp_server
    assert sse.mcp_server is mcp_server
    assert stdio.mcp_server is mcp_server


# ── stdio ────────────────────────────────────────────────────────────────────


def test_stdio_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert authenticate_from_env() is None


def test_stdio_refuses_an_invalid_key(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "ap_not_a_real_key")
    assert authenticate_from_env() is None


def test_stdio_resolves_a_valid_key_to_its_org(monkeypatch, session, api_key_record):
    record, raw_key = api_key_record
    monkeypatch.setenv(ENV_VAR, raw_key)
    auth = authenticate_from_env()
    assert auth is not None
    assert auth.org.id == record.org_id
    assert auth.api_key is not None
    assert auth.user is None


def test_stdio_refuses_a_revoked_key(monkeypatch, session, api_key_record):
    record, raw_key = api_key_record
    record.is_active = False
    session.add(record)
    session.commit()
    monkeypatch.setenv(ENV_VAR, raw_key)
    assert authenticate_from_env() is None
