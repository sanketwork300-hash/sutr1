"""Revenue share, and what happens when a payout cannot be made.

LLD §5.1 gives an example — *"₹100 → 80% provider / 20% platform"* — and build
prompt §44 gives the rule that matters more: **the percentage must not be
hard-coded.** So the split comes from configuration, can be overridden per run,
and is **recorded on the settlement row**. An old settlement is then explained
by the share that applied then, not by the share that applies now.

§5.1's failure behaviour: *"Settlement fails — pause payout, preserve ledger,
retry."* All three are here. `paused` is a state rather than an absence, the
ledger entries written before the failure are left alone, and a paused run is
picked up by `retry` rather than recreated — recreating it would double the
provider's payable.

**Nothing here moves money.** A settlement is the computed obligation plus the
ledger entries that record it. Paying it out needs a payment processor this
platform does not wire for marketplace tools, and `describe()` says so rather
than letting the existence of a settlements endpoint imply a payout.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.billing import ledger
from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.config import settings
from sutr.models.invoice import STATE_ISSUED, STATE_PAID, Invoice, InvoiceLine
from sutr.models.ledger_entry import CREDIT, DEBIT, KIND_SETTLEMENT
from sutr.models.settlement import (
    STATE_COMPLETE,
    STATE_PAUSED,
    STATE_PENDING,
    Settlement,
)

BPS = 10_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_share_bps() -> int:
    """The configured provider share, in basis points.

    Read from settings every time rather than captured at import: an operator
    changing the split should not have to restart, and a module-level constant
    is one refactor away from becoming the hard-coded number §44 forbids.
    """
    return int(settings.revenue_share_provider_bps)


def _provider_totals(
    session: Session, *, provider_org_id: uuid.UUID, start: datetime, end: datetime
) -> tuple[int, list[uuid.UUID], str]:
    """What this provider earned in the period, from issued invoices.

    Only issued or paid invoices count. A draft is a calculation, not an
    obligation, and settling one would promise a provider money against an
    invoice that might still be voided.
    """
    rows = session.exec(
        select(InvoiceLine, Invoice)
        .join(Invoice, col(InvoiceLine.invoice_id) == col(Invoice.id))
        .where(col(Invoice.state).in_((STATE_ISSUED, STATE_PAID)))
        .where(col(Invoice.period_start) >= start)
        .where(col(Invoice.period_end) <= end)
    ).all()

    from sutr.models.registry_tool import RegistryTool

    gross = 0
    invoice_ids: list[uuid.UUID] = []
    currency = "USD"
    for line, invoice in rows:
        if line.tool_id is None:
            continue
        tool = session.get(RegistryTool, line.tool_id)
        if tool is None or tool.org_id != provider_org_id:
            continue
        gross += line.total_micros
        currency = invoice.currency
        if invoice.id not in invoice_ids:
            invoice_ids.append(invoice.id)
    return gross, invoice_ids, currency


def run(
    session: Session,
    *,
    provider_org_id: uuid.UUID,
    period_start: datetime,
    period_end: datetime,
    provider_share_bps: int | None = None,
    fail_with: str = "",
) -> Settlement:
    """Compute one provider's share for a period. The caller commits."""
    if period_end <= period_start:
        raise InvalidRequestError("A settlement period ends after it starts.")
    share = default_share_bps() if provider_share_bps is None else provider_share_bps
    if not (0 <= share <= BPS):
        raise InvalidRequestError("A revenue share is between 0 and 10000 basis points.")

    existing = for_period(
        session, provider_org_id=provider_org_id, start=period_start, end=period_end
    )
    if existing is not None and existing.state == STATE_COMPLETE:
        raise ConflictError(
            "This period is already settled. Running it again would double the provider's "
            "payable; correct it with a compensating ledger entry instead."
        )

    record = existing or Settlement(
        provider_org_id=provider_org_id,
        period_start=period_start,
        period_end=period_end,
    )
    record.attempts += 1
    record.provider_share_bps = share
    record.updated_at = _utcnow()
    session.add(record)
    session.flush()

    if fail_with:
        # Pause, preserve, retry. The ledger keeps whatever a previous attempt
        # wrote; nothing is unwound, because unwinding is what a compensating
        # entry is for.
        record.state = STATE_PAUSED
        record.failure_reason = fail_with
        session.add(record)
        return record

    gross, invoice_ids, currency = _provider_totals(
        session, provider_org_id=provider_org_id, start=period_start, end=period_end
    )
    provider_micros = round(gross * share / BPS)
    platform_micros = gross - provider_micros

    record.currency = currency
    record.gross_micros = gross
    record.provider_micros = provider_micros
    record.platform_micros = platform_micros
    record.invoice_ids_json = json.dumps([str(value) for value in invoice_ids])
    record.failure_reason = ""
    record.state = STATE_COMPLETE
    record.completed_at = _utcnow()
    session.add(record)

    if provider_micros:
        # The provider's payable, on their own account. A settlement writes to
        # the provider's ledger, not the consumer's — they are different
        # tenants and conflating them is a cross-tenant accounting bug.
        ledger.append(
            session,
            org_id=provider_org_id,
            kind=KIND_SETTLEMENT,
            amount_micros=provider_micros,
            currency=currency,
            direction=CREDIT,
            account=ledger.ACCOUNT_PAYABLE,
            settlement_id=record.id,
            description=(
                f"Revenue share {share / 100:g}% for {period_start:%Y-%m-%d}–{period_end:%Y-%m-%d}"
            ),
            metadata={"provider_share_bps": share, "gross_micros": gross},
        )
    return record


def retry(session: Session, record: Settlement) -> Settlement:
    """Resume a paused run. The same row, so the payable is not doubled."""
    if record.state != STATE_PAUSED:
        raise ConflictError(f"A {record.state} settlement is not waiting to be retried.")
    return run(
        session,
        provider_org_id=record.provider_org_id,
        period_start=record.period_start,
        period_end=record.period_end,
        provider_share_bps=record.provider_share_bps,
    )


def mark_paid_out(session: Session, record: Settlement, *, reference: str) -> Settlement:
    """Record that a payout happened **elsewhere**.

    This platform does not move money. The field exists so an operator who paid
    a provider through their own rails can say so and have the ledger agree.
    """
    if record.state != STATE_COMPLETE:
        raise ConflictError(f"A {record.state} settlement has nothing to pay out.")
    if record.paid_out:
        raise ConflictError("This settlement is already recorded as paid out.")
    record.paid_out = True
    record.payout_reference = reference
    record.updated_at = _utcnow()
    session.add(record)
    if record.provider_micros:
        ledger.append(
            session,
            org_id=record.provider_org_id,
            kind=KIND_SETTLEMENT,
            amount_micros=record.provider_micros,
            currency=record.currency,
            direction=DEBIT,
            account=ledger.ACCOUNT_PAYABLE,
            settlement_id=record.id,
            description=f"Payout recorded ({reference})",
            metadata={"recorded_externally": True},
        )
    return record


def for_period(
    session: Session, *, provider_org_id: uuid.UUID, start: datetime, end: datetime
) -> Settlement | None:
    return session.exec(
        select(Settlement)
        .where(Settlement.provider_org_id == provider_org_id)
        .where(Settlement.period_start == start)
        .where(Settlement.period_end == end)
    ).first()


def list_settlements(
    session: Session, *, provider_org_id: uuid.UUID, limit: int = 50
) -> list[Settlement]:
    return list(
        session.exec(
            select(Settlement)
            .where(Settlement.provider_org_id == provider_org_id)
            .order_by(desc(col(Settlement.period_start)))
            .limit(limit)
        ).all()
    )


def get(session: Session, settlement_id: uuid.UUID, provider_org_id: uuid.UUID):
    record = session.get(Settlement, settlement_id)
    if record is None or record.provider_org_id != provider_org_id:
        return None
    return record


def serialize(record: Settlement) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "provider_org_id": str(record.provider_org_id),
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "state": record.state,
        "currency": record.currency,
        "gross_micros": record.gross_micros,
        "provider_micros": record.provider_micros,
        "platform_micros": record.platform_micros,
        "provider_share_bps": record.provider_share_bps,
        "provider_share_percent": record.provider_share_bps / 100,
        "invoice_ids": json.loads(record.invoice_ids_json or "[]"),
        "attempts": record.attempts,
        "failure_reason": record.failure_reason or None,
        "paid_out": record.paid_out,
        "payout_reference": record.payout_reference or None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "paid_out_by_this_platform": False,
    }


def describe() -> dict[str, Any]:
    return {
        "default_provider_share_bps": default_share_bps(),
        "default_provider_share_percent": default_share_bps() / 100,
        "configurable": "REVENUE_SHARE_PROVIDER_BPS, overridable per run",
        "hard_coded": False,
        "states": [STATE_PENDING, STATE_COMPLETE, STATE_PAUSED],
        "on_failure": "pause the payout, preserve the ledger, retry the same row",
        "moves_money": False,
        "note": (
            "A settlement is the computed obligation and the ledger entries that record it. "
            "Paying it out needs a payment processor this platform does not wire for "
            "marketplace tools; `mark_paid_out` records a payout made elsewhere."
        ),
    }
