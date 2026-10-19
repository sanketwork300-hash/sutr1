"""S9 + S24 (backend): Google sign-in honours TOTP and binds the callback to a
browser nonce.

- A user with TOTP enabled gets a short-lived `totp_challenge` instead of an
  access token; POST /api/auth/totp-challenge turns it into a session under
  the same limiter and lockout as password login.
- Every callback fragment echoes the nonce the browser started the flow with.
"""

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from sutr.config import settings
from sutr.db import get_session
from sutr.main import app
from sutr.models.user import User
from sutr.rate_limit import ACCOUNT_LOCKOUT_THRESHOLD
from sutr.totp import store_totp_secret

NONCE = "browser-nonce-0123456789abcdef"
LOGIN_URL = f"/api/auth/google/login?nonce={NONCE}"
SUB = "google-sub-123"
EMAIL = "alice@example.com"


class _FakeGoogleAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, data=None, headers=None):
        return httpx.Response(
            200,
            json={"access_token": "g_access_token"},
            request=httpx.Request("POST", url),
        )

    async def get(self, url, headers=None):
        return httpx.Response(
            200,
            json={"sub": SUB, "email": EMAIL, "email_verified": True},
            request=httpx.Request("GET", url),
        )


@pytest.fixture(name="google_env")
async def google_env_fixture(monkeypatch):
    monkeypatch.setattr(settings, "google_login_client_id", "gid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_login_client_secret", "gsecret")
    monkeypatch.setattr(settings, "block_signups", False)
    monkeypatch.setattr(settings, "is_self_hosted", False)
    monkeypatch.setattr("sutr.api.google_login.httpx.AsyncClient", _FakeGoogleAsyncClient)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, session
    app.dependency_overrides.clear()
    session.close()


def _seed_totp_user(session) -> tuple[User, str]:
    user = User(
        email=EMAIL,
        google_sub=SUB,
        hashed_password=None,
        email_verified=True,
        totp_enabled=True,
        totp_confirmed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(user)
    session.flush()
    secret = pyotp.random_base32()
    store_totp_secret(session, user, secret)
    session.commit()
    session.refresh(user)
    return user, secret


async def _run_callback(client) -> dict:
    start = await client.get(LOGIN_URL)
    assert start.status_code == 200
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]
    resp = await client.get(
        f"/api/auth/google/callback?code=code_xyz&state={state}",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{settings.ui_base_url}/login/google/callback#")
    return {k: v[0] for k, v in parse_qs(location.split("#", 1)[1]).items()}


@pytest.mark.anyio
async def test_login_start_requires_a_nonce(google_env):
    client, _ = google_env
    assert (await client.get("/api/auth/google/login")).status_code == 422
    assert (await client.get("/api/auth/google/login?nonce=short")).status_code == 422


@pytest.mark.anyio
async def test_callback_echoes_the_nonce_with_the_access_token(google_env):
    client, _ = google_env
    fragment = await _run_callback(client)
    assert "access_token" in fragment
    assert fragment["nonce"] == NONCE
    assert "totp_challenge" not in fragment


@pytest.mark.anyio
async def test_totp_user_gets_a_challenge_not_a_session(google_env):
    client, session = google_env
    _seed_totp_user(session)

    fragment = await _run_callback(client)

    assert "access_token" not in fragment
    assert fragment["nonce"] == NONCE
    challenge = fragment["totp_challenge"]

    # The challenge is not a bearer credential.
    me = await client.get("/api/users/me", headers={"Authorization": f"Bearer {challenge}"})
    assert me.status_code == 401


@pytest.mark.anyio
async def test_challenge_plus_valid_code_mints_a_single_use_session(google_env):
    client, session = google_env
    user, secret = _seed_totp_user(session)
    challenge = (await _run_callback(client))["totp_challenge"]

    resp = await client.post(
        "/api/auth/totp-challenge",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 200
    access_token = resp.json()["access_token"]

    me = await client.get("/api/users/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL

    # The challenge was burned; a second exchange with a fresh code is refused.
    session.refresh(user)
    user.totp_last_used_step = None
    session.add(user)
    session.commit()
    again = await client.post(
        "/api/auth/totp-challenge",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).at(int(time.time()) + 30)},
    )
    assert again.status_code == 401


@pytest.mark.anyio
async def test_wrong_code_counts_toward_lockout_and_locks(google_env):
    client, session = google_env
    user, secret = _seed_totp_user(session)
    challenge = (await _run_callback(client))["totp_challenge"]

    resp = await client.post(
        "/api/auth/totp-challenge", json={"challenge_token": challenge, "code": "000000"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_invalid"
    session.refresh(user)
    assert user.failed_login_attempts == 1

    for _ in range(ACCOUNT_LOCKOUT_THRESHOLD - 1):
        await client.post(
            "/api/auth/totp-challenge", json={"challenge_token": challenge, "code": "000000"}
        )
    session.refresh(user)
    assert user.locked_until is not None

    # Locked: even the right code is refused until the window passes.
    locked = await client.post(
        "/api/auth/totp-challenge",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) >= 1


@pytest.mark.anyio
async def test_bogus_expired_and_stale_challenges_are_refused(google_env, monkeypatch):
    client, session = google_env
    user, secret = _seed_totp_user(session)
    code = pyotp.TOTP(secret).now()

    bogus = await client.post(
        "/api/auth/totp-challenge", json={"challenge_token": "nope", "code": code}
    )
    assert bogus.status_code == 401

    # Expired: minted with a TTL already in the past.
    monkeypatch.setattr("sutr.auth_tokens.TOTP_CHALLENGE_TTL", timedelta(seconds=-10))
    expired_challenge = (await _run_callback(client))["totp_challenge"]
    monkeypatch.undo()
    expired = await client.post(
        "/api/auth/totp-challenge", json={"challenge_token": expired_challenge, "code": code}
    )
    assert expired.status_code == 401

    # Stale: the user's token_version moved on (password reset) after minting.
    monkeypatch.setattr(settings, "google_login_client_id", "gid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_login_client_secret", "gsecret")
    monkeypatch.setattr("sutr.api.google_login.httpx.AsyncClient", _FakeGoogleAsyncClient)
    challenge = (await _run_callback(client))["totp_challenge"]
    user.token_version += 1
    session.add(user)
    session.commit()
    stale = await client.post(
        "/api/auth/totp-challenge", json={"challenge_token": challenge, "code": code}
    )
    assert stale.status_code == 401
