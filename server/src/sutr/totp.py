"""TOTP (authenticator app) primitives.

The seed lives in the secrets backend (`User.totp_secret_id`); recovery-code
hashes and the last accepted time step live on the User row. Re-enabling a
previously configured account does not generate a new secret — the stored one
is reused.

Every successful verification records the time step it matched, and a step
that already authenticated the user is refused (RFC 6238 §5.2): a code seen
on the wire once cannot be replayed during the rest of its window.
"""

import base64
import hashlib
import hmac
import io
import json
import secrets
import time

import pyotp
import qrcode
from sqlmodel import Session

from sutr.models.user import User
from sutr.secrets.records import get_secret_value, upsert_secret

ISSUER = "Sutr"
RECOVERY_CODE_COUNT = 10
TOTP_SECRET_KIND = "user_totp_secret"

# Accept the previous and next 30s step as well, for client clock drift.
_DRIFT_WINDOW = (0, -1, 1)


def generate_secret() -> str:
    """Return a fresh base32 TOTP secret."""
    return pyotp.random_base32()


def generate_recovery_codes() -> list[str]:
    """Return a list of human-friendly one-time recovery codes (plaintext)."""
    # Two 8-char groups, lowercase hex, joined by a dash: 64 bits of entropy,
    # which is out of reach for an offline attack on the SHA-256 hashes.
    return [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(RECOVERY_CODE_COUNT)]


def _secret_ref(user: User) -> str:
    return f"users/{user.id}/totp"


def store_totp_secret(session: Session, user: User, secret: str) -> None:
    """Write the seed through the secrets backend and point the user at it.

    Clears the legacy plaintext column; the caller commits."""
    record = upsert_secret(
        session,
        org_id=None,
        kind=TOTP_SECRET_KIND,
        ref=_secret_ref(user),
        value=secret,
        secret_id=user.totp_secret_id,
    )
    user.totp_secret_id = record.id
    user.totp_secret = None
    session.add(user)


def load_totp_secret(session: Session, user: User) -> str | None:
    """The user's seed: from the secrets backend, else the legacy plaintext column."""
    if user.totp_secret_id:
        value = get_secret_value(session, user.totp_secret_id)
        if value:
            return value
    return user.totp_secret or None


def has_totp_secret(user: User) -> bool:
    return bool(user.totp_secret_id or user.totp_secret)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def hash_recovery_codes(codes: list[str]) -> str:
    """Hash each recovery code with SHA-256 and serialise to JSON.

    Recovery codes are server-generated high-entropy random tokens, so a slow
    KDF (bcrypt) buys nothing and costs ~2s of setup latency. A single SHA-256
    is the standard choice for random token storage."""
    return json.dumps([_hash_code(c) for c in codes])


def otpauth_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_data_url(uri: str) -> str:
    """Render the otpauth URI as a PNG data URL."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def _normalise_code(code: str) -> str:
    return code.replace(" ", "").replace("-", "").strip()


def match_totp_step(secret: str, code: str, *, now: float | None = None) -> int | None:
    """Return the time step a 6-digit code matches within the drift window, else None.

    Pure: no replay check, no persistence. `verify_totp_code` is the gate
    callers want; this exists so the step can be reasoned about on its own."""
    normalised = _normalise_code(code)
    if not normalised.isdigit() or len(normalised) != 6:
        return None
    totp = pyotp.TOTP(secret)
    at = int(time.time() if now is None else now)
    current_step = at // totp.interval
    matched: int | None = None
    for offset in _DRIFT_WINDOW:
        # Constant-time compare for every candidate, so the response time does
        # not reveal which (if any) step matched.
        if hmac.compare_digest(totp.at(at, offset), normalised) and matched is None:
            matched = current_step + offset
    return matched


def verify_totp_code(session: Session, user: User, code: str, *, now: float | None = None) -> bool:
    """Validate a 6-digit TOTP code against the user's seed and burn its time step.

    Rejects any step at or before the last one that authenticated this user,
    so a code is single-use even inside the drift window. On success the
    accepted step is recorded on the user (caller commits) and a legacy
    plaintext seed is moved into the secrets backend."""
    secret = load_totp_secret(session, user)
    if not secret:
        return False
    step = match_totp_step(secret, code, now=now)
    if step is None:
        return False
    if user.totp_last_used_step is not None and step <= user.totp_last_used_step:
        return False
    user.totp_last_used_step = step
    if user.totp_secret and not user.totp_secret_id:
        # Lazy migration: the seed just proved itself; move it off the row.
        store_totp_secret(session, user, secret)
    session.add(user)
    return True


def consume_recovery_code(user: User, code: str) -> bool:
    """If ``code`` matches one of the user's unused recovery codes, burn it and
    return True. Persists the updated list back onto the user object (caller
    must commit the session)."""
    if not user.totp_recovery_codes_hash_json:
        return False
    normalised = code.strip().lower().replace(" ", "")
    try:
        hashes: list[str] = json.loads(user.totp_recovery_codes_hash_json)
    except json.JSONDecodeError:
        return False
    candidate = _hash_code(normalised)
    for idx, h in enumerate(hashes):
        if hmac.compare_digest(candidate, h):
            hashes.pop(idx)
            user.totp_recovery_codes_hash_json = json.dumps(hashes)
            return True
    return False


def verify_second_factor(session: Session, user: User, code: str | None) -> bool:
    """Verify either a TOTP code or a recovery code. Mutates ``user`` to burn
    the recovery code or the TOTP step on success (caller commits)."""
    if not code:
        return False
    stripped = code.strip()
    # TOTP codes are all digits (optionally with spaces). Anything else is a
    # recovery code attempt.
    if _normalise_code(stripped).isdigit() and has_totp_secret(user):
        if verify_totp_code(session, user, stripped):
            return True
    return consume_recovery_code(user, stripped)
