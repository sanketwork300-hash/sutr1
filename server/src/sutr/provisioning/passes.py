"""Scoped access passes: short-lived, single-purpose, least-privilege, signed, revocable.

LLD §4.3 gives five adjectives and one sentence of provenance:

    Short-lived · single-purpose · least-privilege · signed · revocable —
    conceptually AWS STS temporary credentials. Issued by Provisioning after
    the policy decision.

Each adjective is a property of this module, and each is enforced rather than
aspired to:

- **short-lived** — a ceiling on the requested lifetime, minutes not days.
- **single-purpose** — a pass names its tools and its resource. A pass with no
  tool list is refused; "everything" is not a scope.
- **least-privilege** — the tools are intersected with what the decision
  actually allowed, so asking for more than you may have narrows the pass
  instead of widening it.
- **signed** — a JWT the generated runtimes already validate (`GOVERNANCE_MODE=
  platform`), so the format is not a new invention: it is the one the servers
  Sutr generates have been checking since Phase 5.
- **revocable** — by `jti`, checked by the platform's own verification.

Two things are deliberately *not* claimed.

**The token is never stored.** The row holds the claims, the decision that
produced them, and the identifiers needed to revoke it. A table of live bearer
tokens is a table whose compromise equals compromising every agent at once.

**A generated runtime cannot see a revocation.** It validates offline, by
signature and expiry, exactly as designed — that is what lets it keep working
when the control plane is down (LLD §2.2). Revocation is enforced where the
platform is in the path, and the short lifetime is what bounds the gap. Said
plainly here and in `describe()`, because a "revocable" credential that a
verifier cannot check is the kind of half-truth §83 exists to prevent.
"""

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from sqlmodel import Session, col, desc, select

from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.config import settings
from sutr.models.access_pass import AccessPass
from sutr.provisioning.pdp import Decision

ALGORITHM = "HS256"
ISSUER = "sutr"

# The LLD's "short-lived", made a number. Five minutes by default: long enough
# for an agent to make the call it asked for, short enough that a leaked pass
# is a small window — which matters precisely because an offline verifier
# cannot check revocation.
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600
MIN_TTL_SECONDS = 30

# The most tools one pass may cover. "Single-purpose" with no ceiling is a
# phrase; with one it is a constraint.
MAX_TOOLS = 32

_KEY_INFO = b"sutr/access-pass/v1"


class PassError(Exception):
    """A pass is missing, invalid, expired, revoked, or out of scope."""


def signing_key() -> bytes:
    """The HMAC key passes are signed with.

    An explicit `ACCESS_PASS_SECRET` when the operator sets one. Otherwise
    derived from the platform's JWT secret by HKDF with a distinct info string,
    so passes and session tokens are cryptographically separated even though
    they come from one configured secret — reusing the JWT secret directly
    would mean a pass and a login token were interchangeable to anything that
    only checked the signature.
    """
    explicit = settings.access_pass_secret.strip()
    if explicit:
        return explicit.encode("utf-8")
    return _hkdf(settings.jwt_secret_key.encode("utf-8"), _KEY_INFO, 32)


def _hkdf(secret: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 with an empty salt (RFC 5869)."""
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def audience_for(resource: str) -> str:
    """What the pass is addressed to.

    The resource itself, so a pass minted for one deployment is rejected by
    another: `aud` is the claim every JWT verifier checks, and making it the
    resource turns "single-purpose" into something the verifier enforces rather
    than something the issuer intended.
    """
    return resource or "sutr:platform"


@dataclass
class IssuedPass:
    """The token and its record. The token is returned once and never stored."""

    token: str
    record: AccessPass
    expires_in: int
    granted_tools: list[str] = field(default_factory=list)
    narrowed_from: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = serialize(self.record)
        payload.update(
            {
                "access_pass": self.token,
                "expires_in": self.expires_in,
                "token_type": "Bearer",
                # Said at the moment of issuance, where it is least ignorable.
                "stored": False,
                "storage_note": (
                    "This token is returned once and is not stored. The platform keeps the "
                    "claims and the decision, not the credential."
                ),
            }
        )
        if self.narrowed_from is not None:
            payload["narrowed_from"] = self.narrowed_from
            payload["narrowed_reason"] = (
                "Least privilege: tools you are not entitled to were removed rather than the "
                "request being refused."
            )
        return payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def issue(
    session: Session,
    *,
    org_id: uuid.UUID,
    principal: str,
    decision: Decision,
    tools: list[str],
    resource: str = "",
    purpose: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    agent_id: uuid.UUID | None = None,
    issued_by_user_id: uuid.UUID | None = None,
    entitled_tools: list[str] | None = None,
) -> IssuedPass:
    """Mint a pass **after** a decision. The caller commits.

    `decision` is not decoration: a pass is the record of an authorization
    decision, and issuing one without a decision that allowed it would make the
    audit trail describe something that did not happen. A refused decision
    raises.
    """
    if not decision.allowed:
        raise ConflictError(
            f"A pass cannot be issued for a refused decision ({decision.denied_by}: "
            f"{decision.reason})"
        )
    requested = [tool.strip() for tool in tools if tool and tool.strip()]
    if not requested:
        raise InvalidRequestError(
            "A pass must name the tools it covers. A pass with no tool list would cover "
            "everything, which is the opposite of least privilege."
        )
    if len(requested) > MAX_TOOLS:
        raise InvalidRequestError(
            f"A pass may cover at most {MAX_TOOLS} tools. Issue separate passes: that is what "
            "single-purpose means."
        )
    if not (MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS):
        raise InvalidRequestError(
            f"A pass lives between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds."
        )

    narrowed_from = None
    granted = requested
    if entitled_tools is not None:
        allowed = set(entitled_tools)
        granted = [tool for tool in requested if tool in allowed]
        if granted != requested:
            narrowed_from = requested
        if not granted:
            raise ConflictError(
                "None of the requested tools are covered by this decision, so there is nothing "
                "to issue a pass for."
            )

    now = _utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)
    pass_id = uuid.uuid4()
    nonce = secrets.token_urlsafe(16)
    audience = audience_for(resource)

    claims = {
        "iss": ISSUER,
        "aud": audience,
        "sub": principal,
        "jti": str(pass_id),
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "tenant_id": str(org_id),
        "tools": granted,
        "purpose": purpose,
        "resource": resource,
    }
    token = jwt.encode(claims, signing_key(), algorithm=ALGORITHM)

    record = AccessPass(
        id=pass_id,
        org_id=org_id,
        principal=principal,
        agent_id=agent_id,
        issued_by_user_id=issued_by_user_id,
        purpose=purpose,
        audience=audience,
        tools_json=json.dumps(granted),
        resource=resource,
        nonce=nonce,
        issued_at=now,
        expires_at=expires_at,
        decision_json=json.dumps(decision.as_dict()),
    )
    session.add(record)
    session.flush()
    return IssuedPass(
        token=token,
        record=record,
        expires_in=ttl_seconds,
        granted_tools=granted,
        narrowed_from=narrowed_from,
    )


def verify(
    session: Session,
    token: str,
    *,
    tool_name: str | None = None,
    resource: str = "",
    record_use: bool = True,
) -> AccessPass:
    """Validate a pass the way the platform can — including revocation.

    A generated runtime validates signature, issuer, audience, expiry and its
    own single-use nonce, offline. This adds the two checks that need the
    platform: that the pass has not been revoked, and that its record still
    exists.
    """
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=[ALGORITHM],
            audience=audience_for(resource),
            issuer=ISSUER,
            options={"require": ["exp", "iss", "aud", "sub", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise PassError(f"Access pass rejected: {exc}")

    record = session.get(AccessPass, uuid.UUID(claims["jti"]))
    if record is None:
        # A well-formed pass with no record is a pass this platform did not
        # issue, or one whose record was removed. Either way it is not usable.
        raise PassError("Access pass is not on record.")
    if record.revoked_at is not None:
        raise PassError(f"Access pass was revoked: {record.revoked_reason or 'no reason given'}.")
    if tool_name is not None:
        covered = json.loads(record.tools_json or "[]")
        if tool_name not in covered:
            raise PassError(f"Access pass does not cover the tool '{tool_name}'.")

    if record_use:
        record.use_count += 1
        record.first_seen_at = record.first_seen_at or _utcnow()
        session.add(record)
    return record


def revoke(session: Session, record: AccessPass, *, reason: str = "") -> AccessPass:
    if record.revoked_at is not None:
        raise ConflictError("That pass was already revoked.")
    record.revoked_at = _utcnow()
    record.revoked_reason = reason
    session.add(record)
    return record


def revoke_for_principal(
    session: Session, *, org_id: uuid.UUID, principal: str, reason: str
) -> int:
    """Revoke every live pass held by one principal. Returns how many.

    The operation an incident needs: a leaked agent credential is not revoked
    by deleting the key, because the passes it already minted outlive it.
    """
    now = _utcnow()
    live = session.exec(
        select(AccessPass)
        .where(AccessPass.org_id == org_id)
        .where(AccessPass.principal == principal)
        .where(col(AccessPass.revoked_at).is_(None))
        .where(col(AccessPass.expires_at) > now)
    ).all()
    for record in live:
        record.revoked_at = now
        record.revoked_reason = reason
        session.add(record)
    return len(live)


def get(session: Session, pass_id: uuid.UUID, org_id: uuid.UUID) -> AccessPass | None:
    record = session.get(AccessPass, pass_id)
    if record is None or record.org_id != org_id:
        return None
    return record


def list_passes(
    session: Session, *, org_id: uuid.UUID, principal: str | None = None, limit: int = 50
) -> list[AccessPass]:
    statement = select(AccessPass).where(AccessPass.org_id == org_id)
    if principal:
        statement = statement.where(AccessPass.principal == principal)
    return list(
        session.exec(statement.order_by(desc(col(AccessPass.issued_at))).limit(limit)).all()
    )


def status_of(record: AccessPass, *, now: datetime | None = None) -> str:
    moment = now or _utcnow()
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if record.revoked_at is not None:
        return "revoked"
    if expires <= moment:
        return "expired"
    return "active"


def serialize(record: AccessPass) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "principal": record.principal,
        "agent_id": str(record.agent_id) if record.agent_id else None,
        "purpose": record.purpose or None,
        "audience": record.audience,
        "resource": record.resource or None,
        "tools": json.loads(record.tools_json or "[]"),
        "status": status_of(record),
        "issued_at": record.issued_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "first_seen_at": record.first_seen_at.isoformat() if record.first_seen_at else None,
        "use_count": record.use_count,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "revoked_reason": record.revoked_reason or None,
        "decision": json.loads(record.decision_json or "{}"),
    }


def describe() -> dict[str, Any]:
    configured = bool(settings.access_pass_secret.strip())
    return {
        "algorithm": ALGORITHM,
        "issuer": ISSUER,
        "default_ttl_seconds": DEFAULT_TTL_SECONDS,
        "max_ttl_seconds": MAX_TTL_SECONDS,
        "max_tools_per_pass": MAX_TOOLS,
        "signing_key": {
            "configured": configured,
            "detail": (
                "ACCESS_PASS_SECRET is set."
                if configured
                else "Derived from JWT_SECRET_KEY by HKDF, so passes and session tokens are "
                "cryptographically separated. Set ACCESS_PASS_SECRET to control it directly."
            ),
        },
        "revocation": {
            "enforced_by_platform": True,
            "enforced_by_generated_runtimes": False,
            "detail": (
                "A generated MCP server validates a pass offline — signature, issuer, audience, "
                "expiry and its own single-use nonce — which is what lets it keep serving when "
                "the control plane is down. It cannot see a revocation, so the short lifetime "
                "is what bounds that gap."
            ),
        },
        "token_storage": "none - the platform stores the claims and the decision, not the token",
    }
