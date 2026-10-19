"""S13 + S19: TOTP replay protection, second-factor lockout, seed storage and
recovery-code strength."""

import re
import time
from datetime import datetime

import pyotp
import pytest
from sqlmodel import select

from sutr.models.secret import Secret
from sutr.rate_limit import ACCOUNT_LOCKOUT_THRESHOLD
from sutr.security import hash_password
from sutr.totp import (
    TOTP_SECRET_KIND,
    generate_recovery_codes,
    load_totp_secret,
    store_totp_secret,
    verify_totp_code,
)


def _enable_totp_in_backend(session, user) -> str:
    secret = pyotp.random_base32()
    store_totp_secret(session, user, secret)
    user.totp_enabled = True
    user.totp_confirmed_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return secret


def _enable_totp_legacy(session, user) -> str:
    secret = pyotp.random_base32()
    user.totp_secret = secret
    user.totp_enabled = True
    user.totp_confirmed_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return secret


# ── Replay ───────────────────────────────────────────────────────────────────


def test_a_code_is_accepted_once(session, test_user):
    secret = _enable_totp_in_backend(session, test_user)
    code = pyotp.TOTP(secret).now()

    assert verify_totp_code(session, test_user, code) is True
    assert test_user.totp_last_used_step is not None
    assert verify_totp_code(session, test_user, code) is False


def test_a_step_before_the_last_accepted_one_is_rejected(session, test_user):
    secret = _enable_totp_in_backend(session, test_user)
    base = int(time.time())
    totp = pyotp.TOTP(secret)

    assert verify_totp_code(session, test_user, totp.at(base + 30)) is True
    # Still inside the drift window, but older than what already authenticated.
    assert verify_totp_code(session, test_user, totp.at(base)) is False


def test_a_later_step_is_still_accepted(session, test_user):
    secret = _enable_totp_in_backend(session, test_user)
    base = int(time.time())
    totp = pyotp.TOTP(secret)

    assert verify_totp_code(session, test_user, totp.at(base)) is True
    assert verify_totp_code(session, test_user, totp.at(base + 30)) is True


# ── Seed storage ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_setup_writes_the_seed_through_the_secrets_backend(client, session, test_user):
    resp = await client.post("/api/users/me/totp/setup")
    assert resp.status_code == 200

    session.refresh(test_user)
    assert test_user.totp_secret is None
    row = session.get(Secret, test_user.totp_secret_id)
    assert row is not None
    assert row.kind == TOTP_SECRET_KIND
    assert row.org_id is None
    assert load_totp_secret(session, test_user) == resp.json()["secret"]


def test_legacy_plaintext_seed_moves_to_the_backend_on_first_verify(session, test_user):
    secret = _enable_totp_legacy(session, test_user)
    assert test_user.totp_secret_id is None

    assert verify_totp_code(session, test_user, pyotp.TOTP(secret).now()) is True
    session.commit()
    session.refresh(test_user)

    assert test_user.totp_secret is None
    assert test_user.totp_secret_id is not None
    assert load_totp_secret(session, test_user) == secret
    assert session.exec(select(Secret).where(Secret.kind == TOTP_SECRET_KIND)).one()


def test_recovery_codes_carry_64_bits():
    for code in generate_recovery_codes():
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{8}", code), code


# ── Lockout on every second-factor gate ──────────────────────────────────────


@pytest.mark.anyio
async def test_second_factor_failures_lock_the_account(client, session, test_user):
    secret = _enable_totp_in_backend(session, test_user)

    for attempt in range(1, ACCOUNT_LOCKOUT_THRESHOLD + 1):
        resp = await client.put(
            "/api/tool-settings/github/create_issue",
            json={"mode": "allow", "totp_code": "000000"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "totp_invalid"
        session.refresh(test_user)
        assert test_user.failed_login_attempts == attempt

    assert test_user.locked_until is not None

    # Locked: the right code is refused with a Retry-After, and the setting is unchanged.
    resp = await client.put(
        "/api/tool-settings/github/create_issue",
        json={"mode": "allow", "totp_code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1
    assert (await client.get("/api/tool-settings/github")).json() == []


@pytest.mark.anyio
async def test_second_factor_success_resets_the_counter(client, session, test_user):
    secret = _enable_totp_in_backend(session, test_user)
    test_user.failed_login_attempts = 3
    session.add(test_user)
    session.commit()

    resp = await client.put(
        "/api/tool-settings/github/create_issue",
        json={"mode": "allow", "totp_code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 200
    session.refresh(test_user)
    assert test_user.failed_login_attempts == 0


@pytest.mark.anyio
async def test_totp_disable_with_wrong_code_counts_as_a_failure(client, session, test_user):
    _enable_totp_in_backend(session, test_user)

    resp = await client.post("/api/users/me/totp/disable", json={"code": "000000"})
    assert resp.status_code == 403
    session.refresh(test_user)
    assert test_user.failed_login_attempts == 1
    assert test_user.totp_enabled is True


@pytest.mark.anyio
async def test_login_totp_failure_counts_and_success_resets(client, session, test_user):
    test_user.hashed_password = hash_password("correct-horse")
    test_user.email_verified = True
    secret = _enable_totp_in_backend(session, test_user)

    # Correct password, wrong code: counts against the account.
    resp = await client.post(
        "/api/auth/token",
        data={"username": test_user.email, "password": "correct-horse", "totp_code": "000000"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_invalid"
    session.refresh(test_user)
    assert test_user.failed_login_attempts == 1

    # Correct password, no code yet: a prompt, not a failure.
    resp = await client.post(
        "/api/auth/token",
        data={"username": test_user.email, "password": "correct-horse"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_required"
    session.refresh(test_user)
    assert test_user.failed_login_attempts == 1

    # Both factors: session issued, counters cleared.
    resp = await client.post(
        "/api/auth/token",
        data={
            "username": test_user.email,
            "password": "correct-horse",
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    session.refresh(test_user)
    assert test_user.failed_login_attempts == 0
    assert test_user.locked_until is None


@pytest.mark.anyio
async def test_login_totp_guessing_locks_the_account(client, session, test_user):
    test_user.hashed_password = hash_password("correct-horse")
    test_user.email_verified = True
    secret = _enable_totp_in_backend(session, test_user)

    for _ in range(ACCOUNT_LOCKOUT_THRESHOLD):
        await client.post(
            "/api/auth/token",
            data={"username": test_user.email, "password": "correct-horse", "totp_code": "000000"},
        )

    resp = await client.post(
        "/api/auth/token",
        data={
            "username": test_user.email,
            "password": "correct-horse",
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert resp.status_code == 429
