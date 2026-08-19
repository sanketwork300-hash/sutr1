"""Regression tests for the findings in review-comments.md.

Each test names its finding so a future reintroduction fails loudly with the
original reasoning attached, rather than being rediscovered by another review.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from sutr import api_client
from sutr.integrations.types import ApiTool, Param
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration import InstalledIntegration
from sutr.secrets.records import upsert_secret

SAVED_BASE = "https://api.saved.example.com"
ATTACKER_BASE = "https://api.attacker.example.com"


def _safe_url_stub(url, **kwargs):
    """Skip DNS/IP screening but keep the real return shape.

    Callers use the result's `.hostname` (e.g. for the audit log line), so the
    stub must be a SafeUpstreamURL-like object, not the raw string.
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    return SimpleNamespace(
        url=url,
        hostname=parsed.hostname,
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        scheme=parsed.scheme,
    )


def _tool(name: str = "get_thing") -> dict:
    return {
        "name": name,
        "description": "Fetch a thing.",
        "method": "GET",
        "path": "/things",
        "params": [],
    }


@pytest.fixture(name="saved_api")
def saved_api_fixture(session, test_org, monkeypatch):
    """A saved custom API with a stored token on its installed row."""
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    row = CustomApiIntegration(
        org_id=test_org.id,
        integration_id="customapi_saved",
        name="Saved API",
        description="Original description",
        base_url=SAVED_BASE,
        token_header="Authorization",
        token_format="Bearer {token}",
        tools_json=json.dumps([_tool()]),
    )
    session.add(row)
    session.flush()
    secret = upsert_secret(
        session,
        org_id=test_org.id,
        kind="integration_token",
        ref=f"integrations/{test_org.id}/customapi_saved/token",
        value="sk_live_THE_REAL_SECRET",
    )
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="customapi_saved",
            type="custom",
            url=SAVED_BASE,
            auth_method="token",
            connected=True,
            token_secret_id=secret.id,
        )
    )
    session.commit()
    session.refresh(row)
    return row


# ── [P1] Stored test tokens must not be sent to arbitrary targets ────────────


async def test_p1_stored_token_is_not_leaked_to_a_swapped_base_url(
    client, session, test_org, saved_api
):
    """review-comments.md [P1] custom_api.py:401-406.

    Omitting `token` while pointing `base_url` at a domain the caller controls
    must NOT cause Sutr to attach the saved integration's secret.
    """
    captured: dict = {}

    async def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"content": [], "isError": False, "status_code": 200, "duration_ms": 1}

    with patch("sutr.api.custom_api.api_client.dispatch_api_tool", side_effect=fake_dispatch):
        resp = await client.post(
            "/api/integrations/custom-api/test",
            json={
                "base_url": ATTACKER_BASE,  # different target …
                "token_header": "Authorization",
                "token_format": "Bearer {token}",
                "tool": _tool(),
                "args": {},
                "integration_db_id": str(saved_api.id),  # … but the saved id
            },
        )

    assert resp.status_code == 200
    headers = captured.get("headers") or {}
    assert "THE_REAL_SECRET" not in json.dumps(headers)
    assert captured["base_url"] == ATTACKER_BASE


async def test_p1_stored_token_is_used_for_the_matching_target(
    client, session, test_org, saved_api
):
    """The mitigation must not break the legitimate case it protects: testing
    the same connection that owns the secret still authenticates."""
    captured: dict = {}

    async def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"content": [], "isError": False, "status_code": 200, "duration_ms": 1}

    with patch("sutr.api.custom_api.api_client.dispatch_api_tool", side_effect=fake_dispatch):
        resp = await client.post(
            "/api/integrations/custom-api/test",
            json={
                "base_url": SAVED_BASE,
                "token_header": "Authorization",
                "token_format": "Bearer {token}",
                "tool": _tool(),
                "args": {},
                "integration_db_id": str(saved_api.id),
            },
        )

    assert resp.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer sk_live_THE_REAL_SECRET"


# ── [P1] Dispatch-time URL revalidation ──────────────────────────────────────


async def test_p1_dispatch_revalidates_the_full_url():
    """review-comments.md [P1] api_client.py:233-240.

    A hostname that passed validation at save time but resolves to a blocked
    address at call time must be rejected when the request is dispatched, not
    trusted because it was checked earlier.
    """
    from sutr.upstream_safety import UnsafeUpstreamUrlError

    tool = ApiTool(
        name="get_thing",
        description="d",
        method="GET",
        path="/things/{id}",
        params=[Param(name="id", type="string", required=True, location="path")],
    )

    def rebound(url, **kwargs):
        raise UnsafeUpstreamUrlError("resolves to a private address")

    with patch("sutr.api_client.validate_safe_url", side_effect=rebound):
        with patch("httpx.AsyncClient.stream") as stream:
            result = await api_client.dispatch_api_tool(
                base_url="https://rebound.example.com",
                tool_def=tool,
                args={"id": "42"},
                headers={},
            )
            stream.assert_not_called()  # refused before any connection

    assert result["isError"] is True
    assert "Unsafe upstream URL" in result["content"][0]["text"]


# ── [P2] Auth-mode switches validate the merged pair ────────────────────────


async def test_p2_switching_to_no_auth_is_allowed(client, session, test_org, saved_api):
    """review-comments.md [P2] custom_api.py:331-337.

    Moving token auth → no auth sends both fields as ""; validating each against
    the other's OLD value rejected the transition.
    """
    resp = await client.patch(
        f"/api/integrations/custom-api/{saved_api.id}",
        json={"token_header": "", "token_format": ""},
    )
    assert resp.status_code == 200, resp.text

    session.expire_all()
    row = session.get(CustomApiIntegration, saved_api.id)
    assert row.token_header == ""
    assert row.token_format == ""


async def test_p2_switching_to_token_auth_is_allowed(client, session, test_org, monkeypatch):
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    row = CustomApiIntegration(
        org_id=test_org.id,
        integration_id="customapi_noauth",
        name="No auth API",
        base_url=SAVED_BASE,
        token_header="",
        token_format="",
        tools_json=json.dumps([_tool()]),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    resp = await client.patch(
        f"/api/integrations/custom-api/{row.id}",
        json={"token_header": "X-Api-Key", "token_format": "{token}"},
    )
    assert resp.status_code == 200, resp.text
    session.expire_all()
    assert session.get(CustomApiIntegration, row.id).token_header == "X-Api-Key"


async def test_p2_an_invalid_merged_pair_is_still_rejected(client, session, saved_api):
    """The fix must not become permissiveness: a format without {token} is
    still invalid when a header is present."""
    resp = await client.patch(
        f"/api/integrations/custom-api/{saved_api.id}",
        json={"token_format": "Bearer no-placeholder"},
    )
    assert resp.status_code == 400


# ── [P3] Descriptions can be cleared ────────────────────────────────────────


async def test_p3_explicit_null_clears_the_description(client, session, saved_api):
    """review-comments.md [P3] custom_api.py:324-325.

    The builder sends description: null when the user empties the field; that
    must clear it, not be treated as an omission.
    """
    assert saved_api.description == "Original description"

    resp = await client.patch(
        f"/api/integrations/custom-api/{saved_api.id}", json={"description": None}
    )
    assert resp.status_code == 200
    assert resp.json()["description"] is None

    session.expire_all()
    assert session.get(CustomApiIntegration, saved_api.id).description is None


async def test_p3_omitting_description_leaves_it_untouched(client, session, saved_api):
    """The distinction matters in both directions: an omitted field must not
    wipe the stored value."""
    resp = await client.patch(
        f"/api/integrations/custom-api/{saved_api.id}", json={"name": "Renamed"}
    )
    assert resp.status_code == 200

    session.expire_all()
    row = session.get(CustomApiIntegration, saved_api.id)
    assert row.name == "Renamed"
    assert row.description == "Original description"


async def test_p3_description_can_be_replaced(client, session, saved_api):
    resp = await client.patch(
        f"/api/integrations/custom-api/{saved_api.id}", json={"description": "New text"}
    )
    assert resp.status_code == 200
    session.expire_all()
    assert session.get(CustomApiIntegration, saved_api.id).description == "New text"


# ── The saved definition stays intact after a test call ─────────────────────


async def test_test_endpoint_does_not_mutate_the_saved_row(client, session, saved_api):
    """A dry-run test must be read-only against the stored definition."""
    with patch(
        "sutr.api.custom_api.api_client.dispatch_api_tool",
        new_callable=AsyncMock,
        return_value={"content": [], "isError": False, "status_code": 200, "duration_ms": 1},
    ):
        await client.post(
            "/api/integrations/custom-api/test",
            json={
                "base_url": SAVED_BASE,
                "token_header": "Authorization",
                "token_format": "Bearer {token}",
                "tool": _tool("probe"),
                "args": {},
                "integration_db_id": str(saved_api.id),
            },
        )

    session.expire_all()
    row = session.get(CustomApiIntegration, saved_api.id)
    assert [t["name"] for t in json.loads(row.tools_json)] == ["get_thing"]
    assert row.base_url == SAVED_BASE
    rows = session.exec(select(CustomApiIntegration)).all()
    assert len(rows) == 1
