"""OAuth authorization-server hardening: S14 (single-use codes), S15 (short
MCP access tokens, rate-limited dynamic registration, consent-page facts)."""

import hashlib
import json
import time

import jwt
import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from sutr.config import settings
from sutr.mcp.oauth_provider import AppAuthorizationCode, oauth_provider
from sutr.models.oauth_auth_code import OAuthAuthCode
from sutr.models.oauth_auth_request import OAuthAuthRequest
from sutr.models.oauth_client import OAuthClient
from sutr.rate_limit import oauth_register_ip_limiter

REDIRECT = "http://localhost:9999/callback"


def _client_row(session, client_id: str = "client-1") -> OAuthClient:
    row = OAuthClient(
        client_id=client_id,
        client_name="Some Client",
        redirect_uris_json=json.dumps([REDIRECT]),
        grant_types_json=json.dumps(["authorization_code", "refresh_token"]),
        response_types_json=json.dumps(["code"]),
        token_endpoint_auth_method="none",
        client_id_issued_at=int(time.time()),
    )
    session.add(row)
    session.commit()
    return row


def _client_info(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Some Client",
        redirect_uris=[AnyUrl(REDIRECT)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def _code_row(session, test_org, test_user, code: str = "code-1") -> OAuthAuthCode:
    row = OAuthAuthCode(
        code=code,
        client_id="client-1",
        org_id=test_org.id,
        user_id=test_user.id,
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        code_challenge="challenge",
        scope="mcp",
        expires_at=time.time() + 600,
    )
    session.add(row)
    session.commit()
    return row


def _app_code(row: OAuthAuthCode) -> AppAuthorizationCode:
    return AppAuthorizationCode(
        code=row.code,
        scopes=["mcp"],
        expires_at=row.expires_at,
        client_id=row.client_id,
        code_challenge=row.code_challenge,
        redirect_uri=AnyUrl(row.redirect_uri),
        redirect_uri_provided_explicitly=True,
        user_id=row.user_id,
        org_id=row.org_id,
    )


# ── S14: authorization code is single use ──────────────────────────────────


async def test_an_authorization_code_can_only_be_exchanged_once(session, test_org, test_user):
    _client_row(session)
    row = _code_row(session, test_org, test_user)
    code = _app_code(row)

    first = await oauth_provider.exchange_authorization_code(_client_info(), code)
    assert first.access_token

    with pytest.raises(TokenError) as excinfo:
        await oauth_provider.exchange_authorization_code(_client_info(), code)
    assert excinfo.value.error == "invalid_grant"


async def test_a_code_that_never_existed_mints_nothing(session, test_org, test_user):
    _client_row(session)
    row = OAuthAuthCode(
        code="phantom",
        client_id="client-1",
        org_id=test_org.id,
        user_id=test_user.id,
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        code_challenge="c",
        expires_at=time.time() + 600,
    )
    with pytest.raises(TokenError):
        await oauth_provider.exchange_authorization_code(_client_info(), _app_code(row))


# ── S15a: MCP access tokens are short-lived ────────────────────────────────


async def test_mcp_access_tokens_use_their_own_short_ttl(session, test_org, test_user, monkeypatch):
    monkeypatch.setattr(settings, "jwt_expire_minutes", 60 * 24 * 7)
    monkeypatch.setattr(settings, "mcp_access_token_expire_minutes", 45)
    _client_row(session)
    row = _code_row(session, test_org, test_user)

    tokens = await oauth_provider.exchange_authorization_code(_client_info(), _app_code(row))
    assert tokens.expires_in == 45 * 60
    payload = jwt.decode(
        tokens.access_token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"verify_aud": False},
    )
    assert payload["exp"] - payload["iat"] == 45 * 60
    # The refresh token keeps its own, longer, lifetime.
    refresh = jwt.decode(
        tokens.refresh_token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
    )
    assert refresh["exp"] - refresh["iat"] > 45 * 60


def test_the_default_mcp_access_ttl_is_an_hour():
    assert type(settings).model_fields["mcp_access_token_expire_minutes"].default == 60


# ── S15b: dynamic client registration is rate limited ──────────────────────


async def test_dynamic_registration_is_rate_limited_per_ip(unauthenticated_client):
    body = {"redirect_uris": [REDIRECT], "client_name": "burst"}
    statuses = []
    for _ in range(oauth_register_ip_limiter.max_requests):
        resp = await unauthenticated_client.post("/register", json=body)
        statuses.append(resp.status_code)
    assert 429 not in statuses

    resp = await unauthenticated_client.post("/register", json=body)
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert resp.json()["error"] == "too_many_requests"


async def test_the_registration_limit_does_not_touch_other_routes(unauthenticated_client):
    for _ in range(oauth_register_ip_limiter.max_requests + 1):
        await unauthenticated_client.post("/register", json={})
    resp = await unauthenticated_client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code != 429


# ── S15d: the consent page is told where the code goes and who asked ───────


async def test_session_info_names_the_redirect_and_registration_origin(client, session):
    _client_row(session)
    token = "consent-session-token"
    session.add(
        OAuthAuthRequest(
            session_token_hash=hashlib.sha256(token.encode()).hexdigest(),
            client_id="client-1",
            redirect_uri=REDIRECT,
            redirect_uri_provided_explicitly=True,
            code_challenge="c",
            expires_at=int(time.time()) + 600,
        )
    )
    session.commit()

    resp = await client.get("/api/oauth/authorize/session", params={"session": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["redirect_uri"] == REDIRECT
    assert body["dynamically_registered"] is True
    assert body["client_name"] == "Some Client"
