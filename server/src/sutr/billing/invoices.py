"""Invoice generation, after the fact and never in the way.

LLD §5.1: *"Invoice generation fails — async retry; never blocks metering."*
That is a statement about coupling, and it is satisfied structurally: nothing
in `services/metering.py` imports this module, so a generation failure cannot
reach the execution path. A failed run marks the invoice `failed` with the
reason and increments its attempt count; metering carries on regardless.

An invoice is **reproducible**. The same usage and the same published plans
produce the same invoice, because the price was never stored on the usage —
it is derived here, from whichever plan was published. Every line records the
plan key, version, model and the arithmetic, so a disputed line can be explained
without re-deriving it.

An invoice is never deleted. A wrong one is **voided** and a new one issued, and
both remain — with the ledger entries to match, because a void that left the
ledger alone would leave the balance wrong.
"""

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.billing import ledger, pricing
from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.models.invoice import (
    STATE_DRAFT,
    STATE_FAILED,
    STATE_ISSUED,
    STATE_PAID,
    STATE_UNPAID,
    STATE_VOID,
    Invoice,
    InvoiceLine,
)
from sutr.models.ledger_entry import KIND_TAX, KIND_USAGE_CHARGE
from sutr.models.usage_event import UsageEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


@dataclass
class GenerationResult:
    invoice: Invoice
    lines: list[InvoiceLine]
    unpriced: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            **serialize(self.invoice, detail=True, lines=self.lines),
            # Usage nobody had published a plan for. Reported rather than
            # silently billed at zero or silently dropped: both would be a
            # decision the platform is not entitled to make.
            "unpriced": self.unpriced,
        }


def number_for(org_id: uuid.UUID, period_start: datetime) -> str:
    return f"INV-{period_start:%Y%m}-{str(org_id)[:8]}"


def _usage_in_period(
    session: Session, *, org_id: uuid.UUID, start: datetime, end: datetime
) -> list[UsageEvent]:
    return list(
        session.exec(
            select(UsageEvent)
            .where(UsageEvent.org_id == org_id)
            .where(col(UsageEvent.timestamp) >= start)
            .where(col(UsageEvent.timestamp) < end)
            # Refusals are metered at quantity 0 so they are auditable without
            # being billed; they contribute nothing here by construction.
            .where(col(UsageEvent.quantity) > 0)
        ).all()
    )


def generate(
    session: Session,
    *,
    org_id: uuid.UUID,
    period_start: datetime,
    period_end: datetime,
    fail_with: str = "",
) -> GenerationResult:
    """Price a period's usage into an invoice. The caller commits.

    `fail_with` exercises §5.1's failure path: a generation that fails must
    leave a retryable record rather than nothing, and a behaviour nobody can
    trigger is a behaviour nobody has seen.
    """
    if period_end <= period_start:
        raise InvalidRequestError("A billing period ends after it starts.")

    existing = for_period(session, org_id=org_id, start=period_start, end=period_end)
    if existing is not None and existing.state not in (STATE_FAILED, STATE_VOID):
        raise ConflictError(
            f"Invoice {existing.number} already covers this period. A second invoice for the "
            "same usage would double-bill it; void the first if it is wrong."
        )

    invoice = existing or Invoice(
        org_id=org_id,
        number=number_for(org_id, period_start),
        period_start=period_start,
        period_end=period_end,
    )
    invoice.attempts += 1
    invoice.updated_at = _utcnow()
    session.add(invoice)
    session.flush()

    if fail_with:
        invoice.state = STATE_FAILED
        invoice.failure_reason = fail_with
        session.add(invoice)
        return GenerationResult(invoice=invoice, lines=[], unpriced=[])

    # A retry of a failed invoice starts from a clean slate: leaving the
    # previous attempt's lines would double the total.
    for stale in lines_for(session, invoice.id):
        session.delete(stale)
    session.flush()

    events = _usage_in_period(session, org_id=org_id, start=period_start, end=period_end)
    grouped: dict[tuple, list[UsageEvent]] = defaultdict(list)
    for event in events:
        grouped[(event.kind, event.integration_id, event.tool_id)].append(event)

    lines: list[InvoiceLine] = []
    unpriced: list[dict[str, Any]] = []
    plan_versions: list[dict[str, Any]] = []
    subtotal = discount = tax = total = 0
    currency = "USD"

    for (kind, integration_id, tool_id), group in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1] or "", str(item[0][2] or ""))
    ):
        quantity = sum(event.quantity for event in group)
        plan = pricing.published_for(session, org_id=org_id, usage_kind=kind, tool_id=tool_id)
        if plan is None:
            unpriced.append(
                {
                    "usage_kind": kind,
                    "integration_id": integration_id,
                    "tool_id": str(tool_id) if tool_id else None,
                    "quantity": quantity,
                    "reason": (
                        "No published pricing plan covers this usage, so it is reported rather "
                        "than billed. Publish a plan and regenerate."
                    ),
                }
            )
            continue
        charge = pricing.evaluate(plan, quantity)
        currency = charge.currency
        line = InvoiceLine(
            invoice_id=invoice.id,
            org_id=org_id,
            description=f"{kind} × {quantity}" + (f" ({integration_id})" if integration_id else ""),
            usage_kind=kind,
            integration_id=integration_id,
            tool_id=tool_id,
            quantity=quantity,
            plan_id=plan.id,
            plan_key=plan.key,
            plan_version=plan.version,
            plan_model=plan.model,
            subtotal_micros=charge.subtotal_micros,
            discount_micros=charge.discount_micros,
            tax_micros=charge.tax_micros,
            total_micros=charge.total_micros,
            breakdown_json=json.dumps(charge.as_dict(), sort_keys=True),
        )
        session.add(line)
        lines.append(line)
        subtotal += charge.subtotal_micros
        discount += charge.discount_micros
        tax += charge.tax_micros
        total += charge.total_micros
        plan_versions.append({"key": plan.key, "version": plan.version, "model": plan.model})

    invoice.currency = currency
    invoice.subtotal_micros = subtotal
    invoice.discount_micros = discount
    invoice.tax_micros = tax
    invoice.total_micros = total
    invoice.plan_versions_json = json.dumps(plan_versions)
    invoice.state = STATE_DRAFT
    invoice.failure_reason = ""
    session.add(invoice)
    session.flush()
    return GenerationResult(invoice=invoice, lines=lines, unpriced=unpriced)


def issue(session: Session, invoice: Invoice) -> Invoice:
    """Issue a drafted invoice, writing the ledger entries that record it."""
    if invoice.state != STATE_DRAFT:
        raise ConflictError(f"This invoice is {invoice.state} and cannot be issued.")
    invoice.state = STATE_ISSUED
    invoice.issued_at = _utcnow()
    invoice.updated_at = invoice.issued_at
    session.add(invoice)

    charge = invoice.total_micros - invoice.tax_micros
    if charge:
        ledger.append(
            session,
            org_id=invoice.org_id,
            kind=KIND_USAGE_CHARGE,
            amount_micros=charge,
            currency=invoice.currency,
            invoice_id=invoice.id,
            description=f"Usage for {invoice.number}",
        )
    if invoice.tax_micros:
        ledger.append(
            session,
            org_id=invoice.org_id,
            kind=KIND_TAX,
            amount_micros=invoice.tax_micros,
            currency=invoice.currency,
            invoice_id=invoice.id,
            description=f"Tax on {invoice.number}",
        )
    return invoice


def void(session: Session, invoice: Invoice, *, reason: str) -> Invoice:
    """Void an invoice, reversing every ledger entry it produced.

    Voiding without reversing would leave the balance carrying a charge for an
    invoice that no longer exists — which is exactly the kind of quiet
    inconsistency an append-only ledger is supposed to prevent.
    """
    if invoice.state in (STATE_VOID, STATE_PAID):
        raise ConflictError(f"A {invoice.state} invoice cannot be voided.")
    if not reason.strip():
        raise InvalidRequestError("Voiding an invoice needs a reason.")
    for entry in ledger.entries_for(
        session, org_id=invoice.org_id, invoice_id=invoice.id, limit=1000
    ):
        if entry.reverses_entry_id is not None:
            continue
        ledger.reverse(session, entry, reason=f"{invoice.number} voided: {reason}")
    invoice.state = STATE_VOID
    invoice.voided_at = _utcnow()
    invoice.void_reason = reason
    invoice.updated_at = invoice.voided_at
    session.add(invoice)
    return invoice


def mark_paid(session: Session, invoice: Invoice) -> Invoice:
    if invoice.state not in (STATE_ISSUED, STATE_UNPAID):
        raise ConflictError(f"A {invoice.state} invoice cannot be marked paid.")
    invoice.state = STATE_PAID
    invoice.paid_at = _utcnow()
    invoice.updated_at = invoice.paid_at
    session.add(invoice)
    return invoice


def for_period(
    session: Session, *, org_id: uuid.UUID, start: datetime, end: datetime
) -> Invoice | None:
    return session.exec(
        select(Invoice)
        .where(Invoice.org_id == org_id)
        .where(Invoice.period_start == start)
        .where(Invoice.period_end == end)
    ).first()


def lines_for(session: Session, invoice_id: uuid.UUID) -> list[InvoiceLine]:
    return list(session.exec(select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)).all())


def get(session: Session, invoice_id: uuid.UUID, org_id: uuid.UUID) -> Invoice | None:
    invoice = session.get(Invoice, invoice_id)
    if invoice is None or invoice.org_id != org_id:
        return None
    return invoice


def list_invoices(session: Session, *, org_id: uuid.UUID, limit: int = 50) -> list[Invoice]:
    return list(
        session.exec(
            select(Invoice)
            .where(Invoice.org_id == org_id)
            .order_by(desc(col(Invoice.period_start)))
            .limit(limit)
        ).all()
    )


def retryable(session: Session, *, org_id: uuid.UUID) -> list[Invoice]:
    return list(
        session.exec(
            select(Invoice).where(Invoice.org_id == org_id).where(Invoice.state == STATE_FAILED)
        ).all()
    )


def serialize(
    invoice: Invoice, *, detail: bool = False, lines: list[InvoiceLine] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(invoice.id),
        "number": invoice.number,
        "state": invoice.state,
        "currency": invoice.currency,
        "period_start": invoice.period_start.isoformat(),
        "period_end": invoice.period_end.isoformat(),
        "subtotal_micros": invoice.subtotal_micros,
        "discount_micros": invoice.discount_micros,
        "tax_micros": invoice.tax_micros,
        "total_micros": invoice.total_micros,
        "plan_versions": json.loads(invoice.plan_versions_json or "[]"),
        "attempts": invoice.attempts,
        "failure_reason": invoice.failure_reason or None,
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "voided_at": invoice.voided_at.isoformat() if invoice.voided_at else None,
        "void_reason": invoice.void_reason or None,
        # The line this endpoint must not let a reader assume otherwise about.
        "collected_by_this_platform": False,
    }
    if detail:
        payload["lines"] = [
            {
                "description": line.description,
                "usage_kind": line.usage_kind,
                "integration_id": line.integration_id,
                "quantity": line.quantity,
                "plan": {
                    "key": line.plan_key,
                    "version": line.plan_version,
                    "model": line.plan_model,
                },
                "subtotal_micros": line.subtotal_micros,
                "discount_micros": line.discount_micros,
                "tax_micros": line.tax_micros,
                "total_micros": line.total_micros,
                "breakdown": json.loads(line.breakdown_json or "{}"),
            }
            for line in (lines if lines is not None else [])
        ]
    return payload
