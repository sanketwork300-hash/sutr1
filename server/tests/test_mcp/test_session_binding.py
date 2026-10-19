"""StreamableHTTP sessions are bound to the principal that created them (S1).

A session is a long-lived server task started from the initialize request;
without binding, any credential plus a leaked `mcp-session-id` could drive
another tenant's session, and a revoked key's session could be kept alive by
a different credential. These tests drive the real ASGI wrapper through a
fresh session manager, so both the SDK's owner check and the gateway's own
binding are exercised end to end.
"""

import hashlib
import secrets
import uuid
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from sutr.main import app
from sutr.mcp import asgi as mcp_asgi
from sutr.mcp import server as mcp_server_module
from sutr.mcp.server import mcp_server
from sutr.models.api_key import ApiKey
from sutr.models.org import Org

ACCEPT = {"Accept": "application/json, text/event-stream"}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "binding-test", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@asynccontextmanager
async def _mcp(monkeypatch):
    """A client against the real app with a live session manager.

    The app's lifespan (which runs the module-level manager) is not executed by
    ASGITransport, and a manager cannot be re-run, so each test gets its own.
    Entered inside the test body so the anyio task group is opened and closed
    in the same task.
    """
    manager = StreamableHTTPSessionManager(app=mcp_server, stateless=False, json_response=True)
    monkeypatch.setattr(mcp_asgi, "session_manager", manager)
    mcp_asgi._session_principals.clear()
    mcp_server_module._session_auth.clear()
    async with manager.run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _make_key(session, org_id: uuid.UUID, user_id: uuid.UUID, name: str) -> tuple[ApiKey, str]:
    plain = "ap_" + secrets.token_urlsafe(24)
    row = ApiKey(
        org_id=org_id,
        created_by_user_id=user_id,
        name=name,
        key_prefix=plain[:12],
        key_hash=hashlib.sha256(plain.encode()).hexdigest(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, plain


async def _open_session(client: AsyncClient, key: str) -> str:
    resp = await client.post("/mcp", json=INITIALIZE, headers={**ACCEPT, "X-API-Key": key})
    assert resp.status_code == 200, resp.text
    session_id = resp.headers.get("mcp-session-id")
    assert session_id
    resp = await client.post(
        "/mcp",
        json=INITIALIZED,
        headers={**ACCEPT, "X-API-Key": key, "mcp-session-id": session_id},
    )
    assert resp.status_code == 202, resp.text
    return session_id


async def _list_tools(client: AsyncClient, key: str, session_id: str):
    return await client.post(
        "/mcp",
        json=LIST_TOOLS,
        headers={**ACCEPT, "X-API-Key": key, "mcp-session-id": session_id},
    )


async def test_the_creating_key_can_drive_its_own_session(session, api_key_record, monkeypatch):
    _, key = api_key_record
    async with _mcp(monkeypatch) as client:
        session_id = await _open_session(client, key)
        resp = await _list_tools(client, key, session_id)
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert any(n.startswith("sutr__") for n in names)
        # Both the SDK's binding and ours hold the creator.
        assert mcp_asgi._session_principals[session_id] == f"api_key:{api_key_record[0].id}"
        assert mcp_asgi.session_manager._session_owners[session_id]["subject"] == (
            f"api_key:{api_key_record[0].id}"
        )


async def test_another_orgs_key_cannot_use_the_session(
    session, test_user, api_key_record, monkeypatch
):
    _, victim_key = api_key_record
    other_org = Org(id=uuid.uuid4(), name="Other Org")
    session.add(other_org)
    session.commit()
    _, attacker_key = _make_key(session, other_org.id, test_user.id, "attacker")

    async with _mcp(monkeypatch) as client:
        session_id = await _open_session(client, victim_key)
        resp = await _list_tools(client, attacker_key, session_id)
        # Indistinguishable from "no such session".
        assert resp.status_code == 404
        assert resp.json()["error"]["message"] == "Session not found"
        # And the session is still intact for its owner.
        resp = await _list_tools(client, victim_key, session_id)
        assert resp.status_code == 200


async def test_a_second_key_of_the_same_org_is_a_different_principal(
    session, test_org, test_user, api_key_record, monkeypatch
):
    _, key_a = api_key_record
    _, key_b = _make_key(session, test_org.id, test_user.id, "second")
    async with _mcp(monkeypatch) as client:
        session_id = await _open_session(client, key_a)
        resp = await _list_tools(client, key_b, session_id)
        assert resp.status_code == 404


async def test_a_revoked_keys_session_cannot_be_driven_by_another_credential(
    session, test_user, api_key_record, monkeypatch
):
    victim_row, victim_key = api_key_record
    other_org = Org(id=uuid.uuid4(), name="Other Org")
    session.add(other_org)
    session.commit()
    _, attacker_key = _make_key(session, other_org.id, test_user.id, "attacker")

    async with _mcp(monkeypatch) as client:
        session_id = await _open_session(client, victim_key)

        victim_row.is_active = False
        session.add(victim_row)
        session.commit()

        # The revoked key itself is refused outright...
        resp = await _list_tools(client, victim_key, session_id)
        assert resp.status_code == 401
        # ...and no other credential can take over the session it left behind.
        resp = await _list_tools(client, attacker_key, session_id)
        assert resp.status_code == 404


async def test_the_sdk_owner_check_is_live_for_every_request(session, api_key_record, monkeypatch):
    """Belt and braces: even if the gateway's own table were bypassed, the
    SDK's `_session_owners` comparison must reject a different principal."""
    _, key = api_key_record
    async with _mcp(monkeypatch) as client:
        session_id = await _open_session(client, key)
        # Forget our own binding; only the SDK's remains.
        mcp_asgi._session_principals.clear()
        other_org = Org(id=uuid.uuid4(), name="Other Org")
        session.add(other_org)
        session.commit()
        _, attacker_key = _make_key(
            session, other_org.id, api_key_record[0].created_by_user_id, "x"
        )
        resp = await _list_tools(client, attacker_key, session_id)
        assert resp.status_code == 404


async def test_handlers_see_the_credential_of_the_current_request(
    session, api_key_record, monkeypatch
):
    """The creator's auth is captured in the session task's context; handlers
    must instead act on what this request authenticated as."""
    row, key = api_key_record
    seen: list[str] = []

    async def fake_dispatch(name, arguments, auth):
        seen.append(auth.api_key.name)
        return []

    monkeypatch.setattr(mcp_server_module.management_tools, "dispatch", fake_dispatch)

    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "sutr__probe", "arguments": {}},
    }
    async with _mcp(monkeypatch) as client:
        session_id = await _open_session(client, key)
        headers = {**ACCEPT, "X-API-Key": key, "mcp-session-id": session_id}
        resp = await client.post("/mcp", json=call, headers=headers)
        assert resp.status_code == 200, resp.text

        row.name = "renamed-after-session-start"
        session.add(row)
        session.commit()

        resp = await client.post("/mcp", json=call, headers=headers)
        assert resp.status_code == 200, resp.text

    assert seen == ["test-key", "renamed-after-session-start"]


async def test_stale_bindings_are_pruned_when_sessions_are_created(
    session, api_key_record, monkeypatch
):
    _, key = api_key_record
    async with _mcp(monkeypatch) as client:
        mcp_asgi._session_principals["gone"] = "api_key:nobody"
        mcp_server_module._session_auth["gone"] = object()
        await _open_session(client, key)
        assert "gone" not in mcp_asgi._session_principals
        assert "gone" not in mcp_server_module._session_auth
