"""Connected-account endpoints.

The callback is the security-sensitive surface: it arrives as a bare browser
redirect with no session on it, so the `state` row is the only thing binding
an authorization code to a user. Most of these tests are about that row being
single-use, TTL-bounded, and never guessed at.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel import select

from sutr.models.oauth_connect_state import OAuthConnectState
from sutr.models.provider_connection import ProviderConnection
from sutr.models.user import User


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_id", "gh-client")
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_secret", "gh-secret")
    monkeypatch.setattr("sutr.config.settings.gcp_oauth_client_id", "gcp-client")
    monkeypatch.setattr("sutr.config.settings.gcp_oauth_client_secret", "gcp-secret")
    monkeypatch.setattr("sutr.config.settings.aws_sso_start_url", "https://d-1.awsapps.com/start")
    monkeypatch.setattr("sutr.config.settings.base_url", "https://sutr.example.com")
    monkeypatch.setattr("sutr.config.settings.ui_base_url", "https://app.sutr.example.com")


def _patch_flow(monkeypatch, handler):
    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.connections.flow.httpx.AsyncClient", Patched)


def _github_token_handler(request):
    if "api.github.com" in str(request.url):
        return httpx.Response(200, json={"login": "octocat"})
    return httpx.Response(200, json={"access_token": "gho_abc", "scope": "repo"})


# ── listing ──────────────────────────────────────────────────────────────────


async def test_list_reports_every_provider_and_its_setup_state(client, monkeypatch):
    monkeypatch.setattr("sutr.config.settings.azure_oauth_client_id", "")
    monkeypatch.setattr("sutr.config.settings.azure_oauth_client_secret", "")
    resp = await client.get("/api/connections")
    assert resp.status_code == 200
    by_id = {entry["id"]: entry for entry in resp.json()["providers"]}
    assert set(by_id) == {"github", "gcp", "azure", "aws"}
    assert by_id["github"]["configured"] is True
    assert by_id["github"]["kind"] == "source"
    assert by_id["gcp"]["kind"] == "deploy"
    assert by_id["aws"]["flow"] == "device"
    # An operator's missing setup reads as that, not as a broken button.
    assert by_id["azure"]["configured"] is False
    assert "AZURE_OAUTH_CLIENT_ID" in by_id["azure"]["reason"]
    assert all(entry["connection"] is None for entry in by_id.values())


# ── authorize ────────────────────────────────────────────────────────────────


async def test_authorize_returns_a_url_and_records_a_single_use_state(client, session):
    resp = await client.post("/api/connections/github/authorize", json={"redirect_after": "/x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["flow"] == "authorization_code"
    assert body["authorization_url"].startswith("https://github.com/login/oauth/authorize?")
    assert "code_challenge_method=S256" in body["authorization_url"]

    rows = session.exec(select(OAuthConnectState)).all()
    assert len(rows) == 1
    assert rows[0].provider == "github"
    assert rows[0].code_verifier


async def test_authorize_refuses_an_unconfigured_provider(client, monkeypatch):
    monkeypatch.setattr("sutr.config.settings.gcp_oauth_client_id", "")
    resp = await client.post("/api/connections/gcp/authorize", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "provider_not_configured"


async def test_authorize_rejects_an_unknown_provider(client):
    assert (await client.post("/api/connections/gitlab/authorize", json={})).status_code == 404


# ── callback ─────────────────────────────────────────────────────────────────


async def _start(client, provider="github"):
    await client.post(f"/api/connections/{provider}/authorize", json={"redirect_after": "/x"})


async def test_callback_stores_the_connection_and_redirects_into_the_ui(
    client, session, monkeypatch, test_org, test_user
):
    _patch_flow(monkeypatch, _github_token_handler)
    await _start(client)
    state = session.exec(select(OAuthConnectState)).one().state

    resp = await client.get(
        "/api/connections/github/callback", params={"state": state, "code": "abc"}
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://app.sutr.example.com/x?")
    assert "connection_status=connected" in location

    connection = session.exec(select(ProviderConnection)).one()
    assert connection.provider == "github"
    assert connection.account_label == "octocat"
    assert connection.org_id == test_org.id and connection.user_id == test_user.id
    # The state row is consumed.
    assert session.exec(select(OAuthConnectState)).all() == []


async def test_a_replayed_state_is_refused(client, session, monkeypatch):
    _patch_flow(monkeypatch, _github_token_handler)
    await _start(client)
    state = session.exec(select(OAuthConnectState)).one().state

    first = await client.get(
        "/api/connections/github/callback", params={"state": state, "code": "abc"}
    )
    assert "connection_status=connected" in first.headers["location"]

    replay = await client.get(
        "/api/connections/github/callback", params={"state": state, "code": "abc"}
    )
    assert "connection_error=invalid_state" in replay.headers["location"]
    assert len(session.exec(select(ProviderConnection)).all()) == 1


async def test_an_expired_state_is_refused(client, session, monkeypatch):
    _patch_flow(monkeypatch, _github_token_handler)
    await _start(client)
    row = session.exec(select(OAuthConnectState)).one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    session.add(row)
    session.commit()

    resp = await client.get(
        "/api/connections/github/callback", params={"state": row.state, "code": "abc"}
    )
    assert "authorization_expired" in resp.headers["location"]
    assert session.exec(select(ProviderConnection)).all() == []


async def test_a_denied_authorization_reports_the_provider_reason(client, session):
    await _start(client)
    state = session.exec(select(OAuthConnectState)).one().state
    resp = await client.get(
        "/api/connections/github/callback",
        params={"state": state, "error": "access_denied", "error_description": "User cancelled"},
    )
    assert "connection_status=denied" in resp.headers["location"]
    assert (
        "User+cancelled" in resp.headers["location"]
        or "User%20cancelled" in resp.headers["location"]
    )
    assert session.exec(select(ProviderConnection)).all() == []


async def test_a_state_for_another_provider_is_not_honoured(client, session):
    await _start(client, "github")
    state = session.exec(select(OAuthConnectState)).one().state
    resp = await client.get("/api/connections/gcp/callback", params={"state": state, "code": "abc"})
    assert "connection_error=invalid_state" in resp.headers["location"]


async def test_redirect_after_cannot_leave_the_ui_origin(client, session, monkeypatch):
    """An open redirect on a callback is how an OAuth flow becomes a phishing hop."""
    _patch_flow(monkeypatch, _github_token_handler)
    await client.post(
        "/api/connections/github/authorize",
        json={"redirect_after": "https://evil.example.com/steal"},
    )
    state = session.exec(select(OAuthConnectState)).one().state
    resp = await client.get(
        "/api/connections/github/callback", params={"state": state, "code": "abc"}
    )
    assert resp.headers["location"].startswith("https://app.sutr.example.com/settings?")


async def test_protocol_relative_redirect_after_is_discarded(client, session, monkeypatch):
    _patch_flow(monkeypatch, _github_token_handler)
    await client.post(
        "/api/connections/github/authorize", json={"redirect_after": "//evil.example.com"}
    )
    state = session.exec(select(OAuthConnectState)).one().state
    resp = await client.get(
        "/api/connections/github/callback", params={"state": state, "code": "abc"}
    )
    assert resp.headers["location"].startswith("https://app.sutr.example.com/settings?")


# ── AWS polling ──────────────────────────────────────────────────────────────


async def test_aws_poll_reports_pending_then_connects(client, session, monkeypatch):
    def device_handler(request):
        path = request.url.path
        if path == "/client/register":
            return httpx.Response(200, json={"clientId": "c", "clientSecret": "s"})
        if path == "/device_authorization":
            return httpx.Response(
                200,
                json={
                    "deviceCode": "d",
                    "userCode": "ABCD-EFGH",
                    "verificationUri": "https://device.example",
                    "verificationUriComplete": "https://device.example?user_code=ABCD-EFGH",
                    "expiresIn": 600,
                    "interval": 5,
                },
            )
        if device_handler.calls == 0:
            device_handler.calls += 1
            return httpx.Response(400, json={"error": "AuthorizationPendingException"})
        return httpx.Response(200, json={"accessToken": "sso-token", "expiresIn": 28800})

    device_handler.calls = 0

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(device_handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.connections.device.httpx.AsyncClient", Patched)

    started = await client.post("/api/connections/aws/authorize", json={})
    assert started.status_code == 200
    body = started.json()
    assert body["flow"] == "device"
    assert body["user_code"] == "ABCD-EFGH"

    pending = await client.post("/api/connections/aws/poll", json={"state": body["state"]})
    assert pending.json() == {"status": "pending"}
    # A pending poll must not consume the flow.
    assert len(session.exec(select(OAuthConnectState)).all()) == 1

    done = await client.post("/api/connections/aws/poll", json={"state": body["state"]})
    assert done.json()["status"] == "connected"
    assert done.json()["connection"]["provider"] == "aws"
    assert session.exec(select(OAuthConnectState)).all() == []


async def test_polling_an_unknown_state_is_a_404(client):
    resp = await client.post("/api/connections/aws/poll", json={"state": "made-up"})
    assert resp.status_code == 404


async def test_polling_another_users_flow_is_a_404(
    client, session, test_org, test_user, monkeypatch
):
    def handler(request):
        if request.url.path == "/client/register":
            return httpx.Response(200, json={"clientId": "c", "clientSecret": "s"})
        return httpx.Response(
            200,
            json={
                "deviceCode": "d",
                "userCode": "ABCD-EFGH",
                "verificationUri": "https://device.example",
                "verificationUriComplete": "https://device.example?user_code=ABCD-EFGH",
                "expiresIn": 600,
                "interval": 5,
            },
        )

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.connections.device.httpx.AsyncClient", Patched)

    await client.post("/api/connections/aws/authorize", json={})
    row = session.exec(select(OAuthConnectState)).one()
    other = User(email="other@example.com", hashed_password="x")
    session.add(other)
    session.commit()
    row.user_id = other.id
    session.add(row)
    session.commit()

    resp = await client.post("/api/connections/aws/poll", json={"state": row.state})
    assert resp.status_code == 404


# ── repos, targets, disconnect ───────────────────────────────────────────────


async def test_listing_repos_without_a_connection_says_what_to_do(client):
    resp = await client.get("/api/connections/github/repos")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_connected"


async def test_repos_come_from_the_connected_account(client, session, monkeypatch):
    _patch_flow(monkeypatch, _github_token_handler)
    await _start(client)
    state = session.exec(select(OAuthConnectState)).one().state
    await client.get("/api/connections/github/callback", params={"state": state, "code": "abc"})

    async def fake_list(token, *, query="", limit=60):
        assert token == "gho_abc"
        return [
            {
                "full_name": "acme/api",
                "html_url": "https://github.com/acme/api",
                "private": True,
                "default_branch": "main",
                "description": "",
                "updated_at": None,
            }
        ]

    monkeypatch.setattr("sutr.api.connections.list_github_repositories", fake_list)
    resp = await client.get("/api/connections/github/repos")
    assert resp.status_code == 200
    assert resp.json()["account"] == "octocat"
    assert resp.json()["repositories"][0]["full_name"] == "acme/api"


async def test_disconnect_removes_the_connection(client, session, monkeypatch):
    _patch_flow(monkeypatch, _github_token_handler)
    await _start(client)
    state = session.exec(select(OAuthConnectState)).one().state
    await client.get("/api/connections/github/callback", params={"state": state, "code": "abc"})
    connection = session.exec(select(ProviderConnection)).one()

    resp = await client.delete(f"/api/connections/{connection.id}")
    assert resp.status_code == 204
    assert session.exec(select(ProviderConnection)).all() == []


async def test_a_connection_belonging_to_another_user_is_invisible(
    client, session, test_org, monkeypatch
):
    """A connection is a personal grant; one member must not be able to revoke
    or borrow another's cloud identity."""
    other = User(email="other@example.com", hashed_password="x")
    session.add(other)
    session.commit()
    connection = ProviderConnection(
        org_id=test_org.id, user_id=other.id, provider="gcp", account_label="them@example.com"
    )
    session.add(connection)
    session.commit()

    assert (await client.delete(f"/api/connections/{connection.id}")).status_code == 404
    assert (await client.get(f"/api/connections/{connection.id}/targets")).status_code == 404


async def test_targets_are_refused_for_a_source_provider(client, session, test_org, test_user):
    connection = ProviderConnection(
        org_id=test_org.id, user_id=test_user.id, provider="github", account_label="octocat"
    )
    session.add(connection)
    session.commit()
    resp = await client.get(f"/api/connections/{connection.id}/targets")
    assert resp.status_code == 400
    assert "not a deployment target" in resp.json()["detail"]
