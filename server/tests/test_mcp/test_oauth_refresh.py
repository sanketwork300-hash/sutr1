"""Unit tests for the integration OAuth refresh helpers (previously untested)."""

import uuid
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session

from sutr.mcp.oauth import is_auth_error, is_token_expired, refresh_tokens
from sutr.models.oauth import OAuthState
from sutr.secrets.records import get_secret_value, upsert_secret


def _state(**overrides) -> OAuthState:
    defaults = dict(
        org_id=uuid.uuid4(),
        integration_id="github",
        client_id="client-1",
        token_endpoint="https://auth.example.com/token",
        status="connected",
    )
    defaults.update(overrides)
    return OAuthState(**defaults)


def test_is_token_expired_edges():
    assert not is_token_expired(_state())  # no expiry info → assume non-expiring
    fresh = _state(obtained_at=datetime.utcnow(), expires_in=3600)
    assert not is_token_expired(fresh)
    stale = _state(obtained_at=datetime.utcnow() - timedelta(hours=2), expires_in=3600)
    assert is_token_expired(stale)
    # Within the 60s buffer counts as expired.
    edge = _state(obtained_at=datetime.utcnow() - timedelta(seconds=3570), expires_in=3600)
    assert is_token_expired(edge)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.example.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_is_auth_error_detection():
    assert is_auth_error(_http_error(401))
    assert is_auth_error(_http_error(403))
    assert not is_auth_error(_http_error(500))
    assert not is_auth_error(RuntimeError("boom"))

    grouped = BaseExceptionGroup("g", [ValueError("x"), _http_error(401)])
    assert is_auth_error(grouped)

    chained = RuntimeError("outer")
    chained.__cause__ = _http_error(403)
    assert is_auth_error(chained)

    # Cycle in the chain must not loop forever.
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert not is_auth_error(a)


async def test_refresh_tokens_happy_path(session, test_org, monkeypatch):
    refresh_secret = upsert_secret(
        session,
        org_id=test_org.id,
        kind="integration_oauth_refresh_token",
        ref="t",
        value="old-refresh",
    )
    state = _state(org_id=test_org.id, refresh_token_secret_id=refresh_secret.id)
    session.add(state)
    session.commit()
    session.refresh(state)

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 1800,
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr("sutr.mcp.oauth.httpx.AsyncClient", _FakeClient)

    refreshed = await refresh_tokens(state)
    assert refreshed is not None
    assert refreshed.status == "connected"
    assert refreshed.expires_in == 1800

    from sutr import db as db_module

    with Session(db_module.engine) as check:
        assert get_secret_value(check, refreshed.access_token_secret_id) == "new-access"
        assert get_secret_value(check, refreshed.refresh_token_secret_id) == "new-refresh"


async def test_refresh_tokens_failure_returns_none(session, test_org, monkeypatch):
    refresh_secret = upsert_secret(
        session, org_id=test_org.id, kind="integration_oauth_refresh_token", ref="t", value="rt"
    )
    state = _state(org_id=test_org.id, refresh_token_secret_id=refresh_secret.id)
    session.add(state)
    session.commit()
    session.refresh(state)

    class _FakeResponse:
        status_code = 400
        text = "invalid_grant"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr("sutr.mcp.oauth.httpx.AsyncClient", _FakeClient)
    assert await refresh_tokens(state) is None


async def test_refresh_tokens_missing_material_returns_none(session, test_org):
    state = _state(org_id=test_org.id)  # no refresh token stored
    session.add(state)
    session.commit()
    session.refresh(state)
    assert await refresh_tokens(state) is None
