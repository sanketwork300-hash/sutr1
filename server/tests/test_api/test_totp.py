import time

import pyotp
import pytest

from sutr.totp import generate_recovery_codes, hash_recovery_codes, load_totp_secret


def _two_codes(secret: str) -> tuple[str, str]:
    """Codes for the current and the next 30s step.

    A time step is single-use once it has authenticated the user, so a flow
    that presents two codes must present two different steps; the next one is
    still inside the server's drift window."""
    base = int(time.time())
    totp = pyotp.TOTP(secret)
    return totp.at(base), totp.at(base + 30)


def _forget_used_step(session, user) -> None:
    """Simulate the clock moving on so a previously used step is no longer 'the last one'."""
    user.totp_last_used_step = None
    session.add(user)
    session.commit()


@pytest.mark.anyio
async def test_totp_status_defaults(client):
    resp = await client.get("/api/users/me/totp/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "configured": False}


@pytest.mark.anyio
async def test_totp_setup_returns_provisioning_payload(client, session, test_user):
    resp = await client.post("/api/users/me/totp/setup")
    assert resp.status_code == 200
    data = resp.json()
    assert data["secret"]
    assert data["otpauth_uri"].startswith("otpauth://totp/")
    assert data["qr_data_url"].startswith("data:image/png;base64,")
    assert len(data["recovery_codes"]) == 10

    session.refresh(test_user)
    # The seed goes through the secrets backend, never the plaintext column.
    assert test_user.totp_secret is None
    assert test_user.totp_secret_id is not None
    assert load_totp_secret(session, test_user) == data["secret"]
    assert test_user.totp_enabled is False
    assert test_user.totp_confirmed_at is None
    assert test_user.totp_recovery_codes_hash_json is not None


@pytest.mark.anyio
async def test_totp_enable_confirms_with_valid_code(client, session, test_user):
    setup = (await client.post("/api/users/me/totp/setup")).json()
    code = pyotp.TOTP(setup["secret"]).now()

    resp = await client.post("/api/users/me/totp/enable", json={"code": code})
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "configured": True}

    session.refresh(test_user)
    assert test_user.totp_enabled is True
    assert test_user.totp_confirmed_at is not None


@pytest.mark.anyio
async def test_totp_enable_rejects_invalid_code(client, session, test_user):
    await client.post("/api/users/me/totp/setup")
    resp = await client.post("/api/users/me/totp/enable", json={"code": "000000"})
    assert resp.status_code == 400

    session.refresh(test_user)
    assert test_user.totp_enabled is False


@pytest.mark.anyio
async def test_totp_disable_keeps_secret(client, session, test_user):
    setup = (await client.post("/api/users/me/totp/setup")).json()
    enable_code, disable_code = _two_codes(setup["secret"])
    await client.post("/api/users/me/totp/enable", json={"code": enable_code})

    resp = await client.post("/api/users/me/totp/disable", json={"code": disable_code})
    assert resp.status_code == 200

    session.refresh(test_user)
    assert test_user.totp_enabled is False
    assert load_totp_secret(session, test_user) == setup["secret"]
    assert test_user.totp_confirmed_at is not None


@pytest.mark.anyio
async def test_totp_re_enable_skips_setup(client, session, test_user):
    setup = (await client.post("/api/users/me/totp/setup")).json()
    enable_code, disable_code = _two_codes(setup["secret"])
    await client.post("/api/users/me/totp/enable", json={"code": enable_code})
    await client.post("/api/users/me/totp/disable", json={"code": disable_code})

    _forget_used_step(session, test_user)
    resp = await client.post("/api/users/me/totp/re-enable", json={"code": disable_code})
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "configured": True}

    session.refresh(test_user)
    assert test_user.totp_enabled is True
    assert load_totp_secret(session, test_user) == setup["secret"]


@pytest.mark.anyio
async def test_totp_re_enable_rejected_without_prior_setup(client):
    resp = await client.post("/api/users/me/totp/re-enable", json={"code": "123456"})
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_totp_setup_rejected_when_already_configured(client, session, test_user):
    setup = (await client.post("/api/users/me/totp/setup")).json()
    code = pyotp.TOTP(setup["secret"]).now()
    await client.post("/api/users/me/totp/enable", json={"code": code})

    resp = await client.post("/api/users/me/totp/setup")
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_totp_disable_requires_current_second_factor(client, session, test_user):
    setup = (await client.post("/api/users/me/totp/setup")).json()
    totp = pyotp.TOTP(setup["secret"])
    await client.post("/api/users/me/totp/enable", json={"code": totp.now()})

    resp = await client.post("/api/users/me/totp/disable", json={"code": ""})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_required"

    session.refresh(test_user)
    assert test_user.totp_enabled is True


@pytest.mark.anyio
async def test_totp_re_enable_requires_current_second_factor(client, session, test_user):
    setup = (await client.post("/api/users/me/totp/setup")).json()
    enable_code, disable_code = _two_codes(setup["secret"])
    await client.post("/api/users/me/totp/enable", json={"code": enable_code})
    await client.post("/api/users/me/totp/disable", json={"code": disable_code})

    resp = await client.post("/api/users/me/totp/re-enable", json={"code": "000000"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_invalid"

    session.refresh(test_user)
    assert test_user.totp_enabled is False


@pytest.mark.anyio
async def test_recovery_codes_round_trip():
    codes = generate_recovery_codes()
    assert len(codes) == 10
    payload = hash_recovery_codes(codes)

    # Emulate `consume_recovery_code` by re-loading and matching — just confirm
    # the serialised form round-trips.
    import json as _json

    hashes = _json.loads(payload)
    assert len(hashes) == 10
    for h in hashes:
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


@pytest.mark.anyio
async def test_me_exposes_totp_state(client):
    resp = await client.get("/api/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totp_enabled"] is False
    assert body["totp_configured"] is False
