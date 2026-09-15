"""Time-boxed exceptions (ESDS LLD §5.2.9).

    Violation → Request → Risk Assessment → Decision → Temporary exception →
    Expiry → Revalidation

**All exceptions expire.** `expires_at` is not nullable and there is no "never"
value, because an exception without an end is a policy change nobody wrote down.
The service also caps how far ahead expiry may be set: a column that can hold
2099 satisfies "expires" on paper and nothing in practice.

The risk assessment step is not decoration. A decision recorded without the risk
it accepted is a decision nobody can review later, and "why did we allow this"
is the only question anyone asks about an exception six months on.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import ConflictError, ForbiddenError, InvalidRequestError
from sutr.models.governance_exception import (
    STATE_APPROVED,
    STATE_EXPIRED,
    STATE_REJECTED,
    STATE_REQUESTED,
    STATE_REVOKED,
    GovernanceException,
)

# The longest an exception may run. Ninety days is a quarter: long enough to
# fix the thing properly, short enough that somebody looks at it again in the
# same planning cycle.
MAX_DAYS = 90
DEFAULT_DAYS = 30
# When revalidation falls due, as a fraction of the exception's life. Earlier
# than expiry on purpose: a 90-day exemption reviewed on day 89 was not
# reviewed.
REVALIDATE_AT = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def request(
    session: Session,
    *,
    org_id: uuid.UUID,
    violation: str,
    justification: str,
    control: str = "",
    policy_id: uuid.UUID | None = None,
    scope_type: str = "registry_tool",
    scope_id: str = "",
    days: int = DEFAULT_DAYS,
    risk: dict[str, Any] | None = None,
    requested_by_user_id: uuid.UUID | None = None,
) -> GovernanceException:
    """Raise an exception request. The caller commits."""
    if not violation.strip():
        raise InvalidRequestError(
            "An exception needs the violation it is an exception to. Without it nobody "
            "reviewing this later knows what was allowed."
        )
    if not justification.strip():
        raise InvalidRequestError(
            "An exception needs a justification. 'It was blocking us' is a justification; "
            "silence is not."
        )
    if not (1 <= days <= MAX_DAYS):
        raise InvalidRequestError(
            f"An exception runs between 1 and {MAX_DAYS} days. All exceptions expire, and one "
            "long enough to outlive the problem is a policy change in disguise."
        )
    now = _utcnow()
    expires_at = now + timedelta(days=days)
    record = GovernanceException(
        org_id=org_id,
        policy_id=policy_id,
        control=control,
        scope_type=scope_type,
        scope_id=scope_id,
        violation=violation,
        justification=justification,
        state=STATE_REQUESTED,
        risk_json=json.dumps(risk or {}, sort_keys=True),
        expires_at=expires_at,
        revalidate_at=now + timedelta(days=max(1, int(days * REVALIDATE_AT))),
        requested_by_user_id=requested_by_user_id,
    )
    session.add(record)
    session.flush()
    return record


def assess(
    session: Session, record: GovernanceException, *, assessment: dict[str, Any]
) -> GovernanceException:
    """Attach the risk assessment step, before any decision is taken."""
    if record.state != STATE_REQUESTED:
        raise ConflictError(f"This exception is already {record.state}.")
    record.risk_json = json.dumps(assessment, sort_keys=True)
    record.updated_at = _utcnow()
    session.add(record)
    return record


def decide(
    session: Session,
    record: GovernanceException,
    *,
    approve: bool,
    decided_by_user_id: uuid.UUID | None,
    note: str = "",
) -> GovernanceException:
    """Approve or reject. The requester may not decide their own exception."""
    if record.state != STATE_REQUESTED:
        raise ConflictError(f"This exception is already {record.state}.")
    if decided_by_user_id is not None and record.requested_by_user_id == decided_by_user_id:
        raise ForbiddenError(
            "You requested this exception, so somebody else has to decide it. An exception "
            "self-approved is a control removed."
        )
    if approve and not json.loads(record.risk_json or "{}"):
        raise ConflictError(
            "This exception has no risk assessment. The LLD's flow puts assessment before "
            "decision, and a decision that skipped it cannot be reviewed later."
        )
    record.state = STATE_APPROVED if approve else STATE_REJECTED
    record.decided_by_user_id = decided_by_user_id
    record.decided_at = _utcnow()
    record.decision_note = note
    record.updated_at = record.decided_at
    session.add(record)
    return record


def revoke(
    session: Session, record: GovernanceException, *, reason: str = ""
) -> GovernanceException:
    if record.state not in (STATE_APPROVED, STATE_REQUESTED):
        raise ConflictError(f"This exception is {record.state} and cannot be revoked.")
    record.state = STATE_REVOKED
    record.revoked_reason = reason
    record.updated_at = _utcnow()
    session.add(record)
    return record


def expire_due(session: Session, *, now: datetime | None = None) -> int:
    """Expire approved exceptions past their date. Returns how many.

    An explicit sweep rather than a read-time lapse, for the same reason
    subscriptions use one: an exemption that ends silently is an exemption
    nobody can point at the end of.
    """
    moment = _as_utc(now) or _utcnow()
    due = session.exec(
        select(GovernanceException)
        .where(GovernanceException.state == STATE_APPROVED)
        .where(col(GovernanceException.expires_at) < moment)
    ).all()
    for record in due:
        record.state = STATE_EXPIRED
        record.updated_at = moment
        session.add(record)
    return len(due)


def active_for(
    session: Session, *, org_id: uuid.UUID, scope_id: str, control: str = ""
) -> GovernanceException | None:
    """A live exception covering this scope, if there is one.

    Expiry is checked here as well as by the sweep: an exception that is past
    its date must not be honoured just because nobody has swept yet. The sweep
    is for the record; this is for the decision.
    """
    now = _utcnow()
    statement = (
        select(GovernanceException)
        .where(GovernanceException.org_id == org_id)
        .where(GovernanceException.scope_id == scope_id)
        .where(GovernanceException.state == STATE_APPROVED)
    )
    if control:
        statement = statement.where(GovernanceException.control == control)
    for record in session.exec(statement).all():
        expires = _as_utc(record.expires_at)
        if expires is not None and expires > now:
            return record
    return None


def due_for_revalidation(
    session: Session, *, org_id: uuid.UUID, now: datetime | None = None
) -> list[GovernanceException]:
    moment = _as_utc(now) or _utcnow()
    rows = session.exec(
        select(GovernanceException)
        .where(GovernanceException.org_id == org_id)
        .where(GovernanceException.state == STATE_APPROVED)
    ).all()
    return [
        record
        for record in rows
        if record.revalidate_at is not None and (_as_utc(record.revalidate_at) or moment) <= moment
    ]


def revalidate(
    session: Session, record: GovernanceException, *, days: int, note: str = ""
) -> GovernanceException:
    """Extend an exception after a fresh look, within the same ceiling."""
    if record.state != STATE_APPROVED:
        raise ConflictError(f"This exception is {record.state} and cannot be revalidated.")
    if not (1 <= days <= MAX_DAYS):
        raise InvalidRequestError(f"A revalidation runs between 1 and {MAX_DAYS} days.")
    now = _utcnow()
    record.expires_at = now + timedelta(days=days)
    record.revalidate_at = now + timedelta(days=max(1, int(days * REVALIDATE_AT)))
    record.decision_note = note or record.decision_note
    record.updated_at = now
    session.add(record)
    return record


def get(session: Session, exception_id: uuid.UUID, org_id: uuid.UUID) -> GovernanceException | None:
    record = session.get(GovernanceException, exception_id)
    if record is None or record.org_id != org_id:
        return None
    return record


def list_exceptions(
    session: Session, *, org_id: uuid.UUID, state: str | None = None
) -> list[GovernanceException]:
    statement = select(GovernanceException).where(GovernanceException.org_id == org_id)
    if state:
        statement = statement.where(GovernanceException.state == state)
    return list(session.exec(statement.order_by(desc(col(GovernanceException.created_at)))).all())


def serialize(record: GovernanceException) -> dict[str, Any]:
    expires = _as_utc(record.expires_at)
    return {
        "id": str(record.id),
        "policy_id": str(record.policy_id) if record.policy_id else None,
        "control": record.control or None,
        "scope_type": record.scope_type,
        "scope_id": record.scope_id or None,
        "violation": record.violation,
        "justification": record.justification,
        "state": record.state,
        "risk": json.loads(record.risk_json or "{}"),
        "decision_note": record.decision_note or None,
        "requested_by_user_id": (
            str(record.requested_by_user_id) if record.requested_by_user_id else None
        ),
        "decided_by_user_id": (
            str(record.decided_by_user_id) if record.decided_by_user_id else None
        ),
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
        "expires_at": record.expires_at.isoformat(),
        "expired": expires is not None and expires <= _utcnow(),
        "revalidate_at": record.revalidate_at.isoformat() if record.revalidate_at else None,
        "revoked_reason": record.revoked_reason or None,
        "created_at": record.created_at.isoformat(),
    }


def describe() -> dict[str, Any]:
    return {
        "flow": [
            "violation",
            "request",
            "risk_assessment",
            "decision",
            "temporary_exception",
            "expiry",
            "revalidation",
        ],
        "max_days": MAX_DAYS,
        "default_days": DEFAULT_DAYS,
        "all_expire": True,
        "note": (
            "There is no permanent exception and no 'never' expiry. Approval requires a risk "
            "assessment, and the requester may not decide their own request."
        ),
    }
