"""Payment attempts, and the dunning that follows a failure.

LLD §5.1: *"Payment fails — mark unpaid; dunning workflow."*

Attempts are **rows**, not a counter. The sequence — when each was made, why it
failed, when the next is due — is then legible instead of inferred, which is the
difference between "this invoice has failed three times" and "this invoice
failed on a expired card, then twice on insufficient funds".

**This platform does not collect.** Nothing here charges a card for marketplace
usage: an attempt is *recorded* when something else reports one. Stripe is wired
for the platform's own plan and nothing else, and `describe()` says so rather
than letting a dunning module imply a collector.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, col, select

from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.config import settings
from sutr.models.invoice import STATE_ISSUED, STATE_UNPAID, Invoice
from sutr.models.settlement import PaymentAttempt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def attempts_for(session: Session, invoice_id: uuid.UUID) -> list[PaymentAttempt]:
    return list(
        session.exec(
            select(PaymentAttempt)
            .where(PaymentAttempt.invoice_id == invoice_id)
            .order_by(col(PaymentAttempt.attempt))
        ).all()
    )


def record_attempt(
    session: Session,
    *,
    invoice: Invoice,
    succeeded: bool,
    failure_code: str = "",
    failure_message: str = "",
    processor: str = "external",
    reference: str = "",
) -> PaymentAttempt:
    """Record one collection attempt and move the invoice accordingly.

    A success marks the invoice paid. A failure marks it **unpaid** — which is
    a state, not an absence — and schedules the next attempt, unless the
    attempt ceiling has been reached, at which point the invoice stays unpaid
    with no next attempt and waits for a person.
    """
    if invoice.state not in (STATE_ISSUED, STATE_UNPAID):
        raise ConflictError(f"A {invoice.state} invoice is not awaiting payment.")
    if not succeeded and not failure_code:
        raise InvalidRequestError(
            "A failed payment attempt needs a failure code. 'It did not work' is not something "
            "a dunning workflow can act on."
        )

    previous = attempts_for(session, invoice.id)
    attempt = PaymentAttempt(
        invoice_id=invoice.id,
        org_id=invoice.org_id,
        attempt=len(previous) + 1,
        succeeded=succeeded,
        failure_code=failure_code,
        failure_message=failure_message,
        processor=processor,
        reference=reference,
    )

    if succeeded:
        from sutr.billing import invoices

        invoices.mark_paid(session, invoice)
    else:
        invoice.state = STATE_UNPAID
        invoice.updated_at = _utcnow()
        session.add(invoice)
        if attempt.attempt < settings.dunning_max_attempts:
            attempt.next_attempt_at = _utcnow() + timedelta(hours=settings.dunning_retry_hours)
        # Otherwise `next_attempt_at` stays null: the workflow has done what it
        # can, and an invoice that keeps retrying forever is one nobody looks at.

    session.add(attempt)
    session.flush()
    return attempt


def due(session: Session, *, org_id: uuid.UUID, now: datetime | None = None) -> list[Invoice]:
    """Invoices whose next dunning attempt has come round.

    An explicit sweep, like every other scheduled thing in this platform. There
    is no scheduler calling it, and `describe()` says so.
    """
    moment = _as_utc(now) or _utcnow()
    rows = session.exec(
        select(PaymentAttempt)
        .where(PaymentAttempt.org_id == org_id)
        .where(PaymentAttempt.succeeded == False)  # noqa: E712
        .where(col(PaymentAttempt.next_attempt_at).is_not(None))
    ).all()
    invoices_due: list[Invoice] = []
    seen: set[uuid.UUID] = set()
    for attempt in rows:
        when = _as_utc(attempt.next_attempt_at)
        if when is None or when > moment or attempt.invoice_id in seen:
            continue
        invoice = session.get(Invoice, attempt.invoice_id)
        if invoice is None or invoice.state != STATE_UNPAID:
            continue
        # Only the most recent attempt schedules the next one.
        latest = max(attempts_for(session, invoice.id), key=lambda row: row.attempt)
        if latest.id != attempt.id:
            continue
        seen.add(attempt.invoice_id)
        invoices_due.append(invoice)
    return invoices_due


def exhausted(session: Session, invoice: Invoice) -> bool:
    """Whether dunning has given up on this invoice."""
    rows = attempts_for(session, invoice.id)
    if not rows:
        return False
    latest = max(rows, key=lambda row: row.attempt)
    return not latest.succeeded and latest.next_attempt_at is None


def serialize(attempt: PaymentAttempt) -> dict[str, Any]:
    return {
        "id": str(attempt.id),
        "invoice_id": str(attempt.invoice_id),
        "attempt": attempt.attempt,
        "succeeded": attempt.succeeded,
        "failure_code": attempt.failure_code or None,
        "failure_message": attempt.failure_message or None,
        "next_attempt_at": (
            attempt.next_attempt_at.isoformat() if attempt.next_attempt_at else None
        ),
        "processor": attempt.processor,
        "reference": attempt.reference or None,
        "created_at": attempt.created_at.isoformat(),
    }


def describe() -> dict[str, Any]:
    return {
        "max_attempts": settings.dunning_max_attempts,
        "retry_hours": settings.dunning_retry_hours,
        "collects_payment": False,
        "scheduler": None,
        "note": (
            "Attempts are recorded, not made: nothing here charges a card for marketplace "
            "usage. `due()` finds invoices whose next attempt has come round, and no scheduler "
            "calls it — an operator or a worker does."
        ),
    }
