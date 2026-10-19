"""S4: `POST /api/integrations/custom-api/test` is gated like every other
mutation in that router, and the stored credential is only injected for a
caller who may execute tools."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sutr.models.integration import InstalledIntegration
from sutr.models.org_membership import OrgMembership
from sutr.secrets.records import upsert_secret


def _safe_url_stub(*args, **kwargs):
    return SimpleNamespace(hostname="api.example.com")


def _tool() -> dict:
    return {
        "name": "get_item",
        "description": "Get an item",
        "method": "GET",
        "path": "/v1/items/{id}",
        "params": [{"name": "id", "required": True}],
    }


def _set_role(session, test_user, test_org, role: str) -> None:
    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = role
    session.add(membership)
    session.commit()


def _test_body(row_id: str | None = None) -> dict:
    body = {
        "base_url": "https://api.example.com",
        "token_header": "Authorization",
        "token_format": "Bearer {token}",
        "token": "",
        "tool": _tool(),
        "args": {"id": "1"},
    }
    if row_id:
        body["integration_db_id"] = row_id
    return body


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["viewer", "member"])
async def test_test_endpoint_requires_integrations_manage(
    client, session, test_user, test_org, monkeypatch, role
):
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    calls: list[dict] = []

    async def _dispatch(**kwargs):
        calls.append(kwargs)
        return {"content": [], "isError": False, "status_code": 200, "duration_ms": 1}

    monkeypatch.setattr("sutr.api.custom_api.api_client.dispatch_api_tool", _dispatch)
    _set_role(session, test_user, test_org, role)

    resp = await client.post("/api/integrations/custom-api/test", json=_test_body())

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "permission_denied"
    assert calls == []  # nothing left the server


async def _seed_installed_with_secret(client, session, test_org) -> str:
    create_resp = await client.post(
        "/api/integrations/custom-api",
        json={
            "name": "Same Target",
            "base_url": "https://api.example.com",
            "token_header": "Authorization",
            "token_format": "Bearer {token}",
            "tools": [_tool()],
        },
    )
    row_id = create_resp.json()["id"]
    integration_id = create_resp.json()["integration_id"]
    secret = upsert_secret(
        session,
        org_id=test_org.id,
        kind="integration_token",
        ref=f"integrations/{test_org.id}/{integration_id}/token",
        value="sk_stored_secret",
    )
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id=integration_id,
            type="custom",
            url="https://api.example.com",
            auth_method="token",
            connected=True,
            token_secret_id=secret.id,
        )
    )
    session.commit()
    return row_id


@pytest.mark.anyio
async def test_stored_secret_not_injected_without_tools_execute(
    client, session, test_org, monkeypatch
):
    """The matrix today gives everyone with integrations:manage tools:execute
    too, so the second gate is exercised by making only that permission fail."""
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    row_id = await _seed_installed_with_secret(client, session, test_org)

    from sutr.api import custom_api

    real_ensure = custom_api.ensure_agent_can

    def _no_execute(session_, auth, permission):
        if permission == "tools:execute":
            raise HTTPException(status_code=403, detail={"error": "permission_denied"})
        return real_ensure(session_, auth, permission)

    monkeypatch.setattr("sutr.api.custom_api.ensure_agent_can", _no_execute)

    captured_headers: dict = {}

    async def _dispatch(**kwargs):
        captured_headers.update(kwargs["headers"])
        return {"content": [], "isError": False, "status_code": 200, "duration_ms": 1}

    monkeypatch.setattr("sutr.api.custom_api.api_client.dispatch_api_tool", _dispatch)

    resp = await client.post("/api/integrations/custom-api/test", json=_test_body(row_id))

    assert resp.status_code == 200
    assert "sk_stored_secret" not in captured_headers.get("Authorization", "")


@pytest.mark.anyio
async def test_stored_secret_injected_for_full_permissions(client, session, test_org, monkeypatch):
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    row_id = await _seed_installed_with_secret(client, session, test_org)

    captured_headers: dict = {}

    async def _dispatch(**kwargs):
        captured_headers.update(kwargs["headers"])
        return {"content": [], "isError": False, "status_code": 200, "duration_ms": 1}

    monkeypatch.setattr("sutr.api.custom_api.api_client.dispatch_api_tool", _dispatch)

    resp = await client.post("/api/integrations/custom-api/test", json=_test_body(row_id))

    assert resp.status_code == 200
    assert captured_headers.get("Authorization") == "Bearer sk_stored_secret"
