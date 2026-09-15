"""Idempotency keys: a repeated request returns the original result.

Build prompt §76 lists where this is required — billing, payments,
registration, uploads, settlement, token issuance, tool publication, runtime
deployment. The mechanism is the same everywhere, so it lives here once.

The lifecycle a handler drives:

    claim(...)  →  Replay      the key is known and finished: return it
                →  InProgress  the key is known and still running: 409
                →  Claimed     first time: do the work, then complete(...)

Reserving the key *before* the work runs is what makes two concurrent retries
safe. If the reservation is skipped, two requests both find no record, both do
the work, and the "idempotency" is decoration.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from sutr.common.errors import ConflictError
from sutr.models.idempotency_key import (
    RETENTION_HOURS,
    STATE_COMPLETED,
    STATE_IN_PROGRESS,
    IdempotencyKey,
)

MAX_KEY_LENGTH = 255


def request_hash(payload: Any) -> str:
    """A stable fingerprint of what the key was used for."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass
class Replay:
    """The key was used before and the work finished. Return this."""

    status_code: int
    body: Any


@dataclass
class Claimed:
    """The key is newly reserved. Do the work, then call `complete`."""

    record: IdempotencyKey


def claim(
    session: Session,
    *,
    org_id: uuid.UUID,
    endpoint: str,
    key: str | None,
    payload: Any,
) -> Claimed | Replay | None:
    """Reserve a key, or hand back what it produced last time.

    Returns None when no key was supplied — idempotency is opt-in per request,
    so an existing client that has never sent the header is unaffected.
    """
    if not key:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > MAX_KEY_LENGTH:
        raise ConflictError(
            f"`Idempotency-Key` may not exceed {MAX_KEY_LENGTH} characters.",
            code="invalid_request",
        )

    fingerprint = request_hash(payload)
    existing = session.exec(
        select(IdempotencyKey)
        .where(IdempotencyKey.org_id == org_id)
        .where(IdempotencyKey.endpoint == endpoint)
        .where(IdempotencyKey.key == key)
    ).first()

    if existing is not None:
        if existing.request_hash != fingerprint:
            # Replaying the first response here would silently discard this
            # request. Refusing tells the client about its own bug.
            raise ConflictError(
                "This `Idempotency-Key` was already used for a different request body.",
                details={"idempotency_key": key},
            )
        if existing.state == STATE_COMPLETED:
            return Replay(
                status_code=existing.status_code or 200,
                body=json.loads(existing.response_json) if existing.response_json else None,
            )
        raise ConflictError(
            "A request with this `Idempotency-Key` is still in progress.",
            details={"idempotency_key": key},
            retry_after=1,
        )

    record = IdempotencyKey(
        org_id=org_id,
        endpoint=endpoint,
        key=key,
        request_hash=fingerprint,
        state=STATE_IN_PROGRESS,
        expires_at=datetime.utcnow() + timedelta(hours=RETENTION_HOURS),
    )
    session.add(record)
    try:
        # Committed immediately and on its own: the reservation must be visible
        # to a concurrent retry before the work starts, not after it finishes.
        session.commit()
    except IntegrityError:
        session.rollback()
        # Another request won the race between the SELECT and the INSERT.
        raise ConflictError(
            "A request with this `Idempotency-Key` is still in progress.",
            details={"idempotency_key": key},
            retry_after=1,
        )
    session.refresh(record)
    return Claimed(record=record)


def complete(session: Session, claimed: Claimed | None, *, status_code: int, body: Any) -> None:
    """Record the result so a retry replays it."""
    if claimed is None:
        return
    record = session.get(IdempotencyKey, claimed.record.id)
    if record is None:
        return
    record.state = STATE_COMPLETED
    record.status_code = status_code
    record.response_json = json.dumps(body, default=str)
    session.add(record)
    session.commit()


def release(session: Session, claimed: Claimed | None) -> None:
    """Drop a reservation whose work failed.

    A failed attempt must not lock the key for a day: the client should be able
    to retry and have it actually run.
    """
    if claimed is None:
        return
    record = session.get(IdempotencyKey, claimed.record.id)
    if record is not None:
        session.delete(record)
        session.commit()


def prune_expired(session: Session, *, now: datetime | None = None) -> int:
    """Remove keys past their retention window."""
    cutoff = now or datetime.utcnow()
    result = session.exec(delete(IdempotencyKey).where(col(IdempotencyKey.expires_at) < cutoff))
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
