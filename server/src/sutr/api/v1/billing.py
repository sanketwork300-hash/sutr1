"""Metering, billing and settlement (ESDS LLD §5.1).

§5.1.12 names four routes, and they are these:

    POST /v1/metering/events        record usage
    POST /v1/billing/invoices       generate an invoice for a period
    GET  /v1/billing/invoices/{id}  read one, with its lines and arithmetic
    POST /v1/settlements/run        compute a provider's revenue share

Three prefixes, three routers, one module — the LLD groups them by what they
are about rather than by who serves them, and following its paths is worth more
than tidiness.

**Nothing here collects or pays out money.** Invoices are computed and
settlements are calculated; moving the money needs a payment processor this
platform wires only for its own subscription plan. Every invoice and settlement
response says so in a field rather than leaving a reader to assume.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.billing import dunning, invoices, ledger, pricing, settlement
from sutr.common import envelope
from sutr.common.errors import InvalidRequestError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.models.usage_event import KIND_TOOL_CALL
from sutr.services.audit import actor_from_agent_auth, record_audit
from sutr.services.metering import record_usage

metering_router = APIRouter(prefix="/v1/metering", tags=["metering"])
billing_router = APIRouter(prefix="/v1/billing", tags=["billing"])
settlements_router = APIRouter(prefix="/v1/settlements", tags=["settlements"])

# Recording usage and generating invoices are financial acts; reading them is
# not. `org:manage` already means "manage billing" in this platform's roles.
MANAGE = "org:manage"
METER = "integrations:manage"
READ = "logs:read"


class MeteringEvent(BaseModel):
    """The LLD's `POST /v1/metering/events`.

    There is deliberately **no amount field**. A metered event is a fact about
    what happened; its price is decided at billing time from whichever plan was
    published.
    """

    kind: str = KIND_TOOL_CALL
    quantity: int = Field(default=1, ge=0)
    # The dedupe key. Supply one and a replay is a no-op.
    invocation_id: str | None = Field(default=None, max_length=200)
    integration_id: str | None = None
    tool_name: str | None = None
    tool_id: uuid.UUID | None = None
    provider_org_id: uuid.UUID | None = None
    source: str = "api"
    outcome: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    payload_bytes: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    region: str | None = None
    pricing_context: dict = {}


class PlanRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str = ""
    model: str
    usage_kind: str = KIND_TOOL_CALL
    tool_id: uuid.UUID | None = None
    currency: str = "USD"
    amount_micros: int = Field(default=0, ge=0)
    included_units: int = Field(default=0, ge=0)
    base_micros: int = Field(default=0, ge=0)
    tiers: list[dict] = []
    discount_bps: int = Field(default=0, ge=0, le=10_000)
    tax_bps: int = Field(default=0, ge=0, le=10_000)
    tax_label: str = ""
    notes: str = ""
    publish: bool = False


class InvoiceRequest(BaseModel):
    period_start: datetime
    period_end: datetime
    # Exercises §5.1's *"invoice generation fails — async retry"* path.
    simulate_failure: str = ""
    issue: bool = False


class VoidRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PaymentRequest(BaseModel):
    succeeded: bool
    failure_code: str = ""
    failure_message: str = ""
    processor: str = "external"
    reference: str = ""


class SettlementRequest(BaseModel):
    provider_org_id: uuid.UUID | None = None
    period_start: datetime
    period_end: datetime
    # Overrides the configured share for this run. The percentage is never
    # hard-coded, and this is where a one-off arrangement is expressed.
    provider_share_bps: int | None = Field(default=None, ge=0, le=10_000)
    simulate_failure: str = ""


class PayoutRequest(BaseModel):
    reference: str = Field(min_length=1, max_length=200)


# ── Metering ─────────────────────────────────────────────────────────────────


@metering_router.post("/events", status_code=201)
def record_event(
    body: MeteringEvent,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Record one usage fact. Idempotent when an `invocation_id` is supplied."""
    ensure_agent_can(session, agent_auth, METER)
    before = None
    if body.invocation_id:
        from sutr.services.metering import find_by_invocation

        before = find_by_invocation(
            session, org_id=agent_auth.org.id, invocation_id=body.invocation_id
        )
    event = record_usage(
        session,
        org_id=agent_auth.org.id,
        kind=body.kind,
        quantity=body.quantity,
        integration_id=body.integration_id,
        tool_name=body.tool_name,
        source=body.source,
        outcome=body.outcome,
        duration_ms=body.duration_ms,
        invocation_id=body.invocation_id,
        region=body.region,
        payload_bytes=body.payload_bytes,
        tokens=body.tokens,
        provider_org_id=body.provider_org_id,
        tool_id=body.tool_id,
        pricing_context=body.pricing_context,
        api_key_prefix=agent_auth.api_key.key_prefix if agent_auth.api_key else None,
        user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.commit()
    session.refresh(event)
    return envelope(
        {
            "id": event.id,
            "kind": event.kind,
            "quantity": event.quantity,
            "invocation_id": event.invocation_id,
            "region": event.region,
            "recorded_at": event.timestamp.isoformat(),
            "deduplicated": before is not None,
            # Said at the point where somebody might expect one.
            "priced": False,
            "pricing_note": "Prices are evaluated at billing time, never during execution.",
        }
    )


@metering_router.get("/capabilities")
def metering_capabilities(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What this install can price, settle and collect — and what it cannot."""
    ensure_agent_can(session, agent_auth, READ)
    return envelope(
        {
            "pricing": pricing.describe(),
            "ledger": ledger.describe(),
            "settlement": settlement.describe(),
            "dunning": dunning.describe(),
            "collects_payment": False,
            "note": (
                "Usage is metered, invoices are computed and settlements are calculated. "
                "Moving money needs a payment processor this platform wires only for its own "
                "subscription plan."
            ),
        }
    )


# ── Pricing plans ────────────────────────────────────────────────────────────


@billing_router.post("/plans", status_code=201)
def create_plan(
    body: PlanRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    plan = pricing.create(
        session,
        org_id=agent_auth.org.id,
        key=body.key,
        name=body.name or body.key,
        model=body.model,
        usage_kind=body.usage_kind,
        tool_id=body.tool_id,
        currency=body.currency,
        amount_micros=body.amount_micros,
        included_units=body.included_units,
        base_micros=body.base_micros,
        tiers=body.tiers,
        discount_bps=body.discount_bps,
        tax_bps=body.tax_bps,
        tax_label=body.tax_label,
        notes=body.notes,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    if body.publish:
        pricing.publish(session, plan)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="billing.plan_created",
        summary=f"Pricing plan '{plan.key}' v{plan.version} ({plan.model}) created",
        target_type="pricing_plan",
        target_id=str(plan.id),
        metadata={"model": plan.model, "published": body.publish},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(plan)
    return envelope(pricing.serialize(plan))


@billing_router.get("/plans")
def list_plans(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = pricing.list_plans(session, org_id=agent_auth.org.id)
    return envelope({"plans": [pricing.serialize(plan) for plan in rows], **pricing.describe()})


@billing_router.post("/plans/{plan_id}/publish")
def publish_plan(
    plan_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Publish a plan. Validation happens here, where a mistake is cheap."""
    ensure_agent_can(session, agent_auth, MANAGE)
    plan = pricing.get(session, plan_id, agent_auth.org.id)
    if plan is None:
        raise NotFoundError(f"Pricing plan {plan_id} was not found.")
    pricing.publish(session, plan)
    session.commit()
    session.refresh(plan)
    return envelope(pricing.serialize(plan))


# ── Invoices ─────────────────────────────────────────────────────────────────


@billing_router.post("/invoices", status_code=201)
def generate_invoice(
    body: InvoiceRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Price a period's usage. Metering is untouched whether this works or not."""
    ensure_agent_can(session, agent_auth, MANAGE)
    result = invoices.generate(
        session,
        org_id=agent_auth.org.id,
        period_start=body.period_start,
        period_end=body.period_end,
        fail_with=body.simulate_failure,
    )
    if body.issue and not body.simulate_failure:
        invoices.issue(session, result.invoice)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="billing.invoice_generated",
        summary=f"Invoice {result.invoice.number} {result.invoice.state}",
        target_type="invoice",
        target_id=str(result.invoice.id),
        metadata={"state": result.invoice.state, "total_micros": result.invoice.total_micros},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(result.invoice)
    return envelope(result.as_dict())


@billing_router.get("/invoices")
def list_invoices(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = invoices.list_invoices(session, org_id=agent_auth.org.id, limit=limit)
    return envelope({"invoices": [invoices.serialize(row) for row in rows]})


def _load_invoice(session: Session, invoice_id: uuid.UUID, org_id: uuid.UUID):
    invoice = invoices.get(session, invoice_id, org_id)
    if invoice is None:
        raise NotFoundError(f"Invoice {invoice_id} was not found.")
    return invoice


@billing_router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """One invoice with its lines and the arithmetic behind each."""
    ensure_agent_can(session, agent_auth, READ)
    invoice = _load_invoice(session, invoice_id, agent_auth.org.id)
    lines = invoices.lines_for(session, invoice.id)
    return envelope(
        {
            **invoices.serialize(invoice, detail=True, lines=lines),
            "payment_attempts": [
                dunning.serialize(attempt) for attempt in dunning.attempts_for(session, invoice.id)
            ],
        }
    )


@billing_router.post("/invoices/{invoice_id}/issue")
def issue_invoice(
    invoice_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    invoice = _load_invoice(session, invoice_id, agent_auth.org.id)
    invoices.issue(session, invoice)
    session.commit()
    session.refresh(invoice)
    return envelope(invoices.serialize(invoice))


@billing_router.post("/invoices/{invoice_id}/void")
def void_invoice(
    invoice_id: uuid.UUID,
    body: VoidRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Void an invoice, reversing every ledger entry it produced."""
    ensure_agent_can(session, agent_auth, MANAGE)
    invoice = _load_invoice(session, invoice_id, agent_auth.org.id)
    invoices.void(session, invoice, reason=body.reason)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="billing.invoice_voided",
        summary=f"Invoice {invoice.number} voided",
        target_type="invoice",
        target_id=str(invoice.id),
        metadata={"reason": body.reason[:200]},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(invoice)
    return envelope(invoices.serialize(invoice))


@billing_router.post("/invoices/{invoice_id}/payments", status_code=201)
def record_payment(
    invoice_id: uuid.UUID,
    body: PaymentRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Record a collection attempt made **elsewhere**, and run the dunning step."""
    ensure_agent_can(session, agent_auth, MANAGE)
    invoice = _load_invoice(session, invoice_id, agent_auth.org.id)
    attempt = dunning.record_attempt(
        session,
        invoice=invoice,
        succeeded=body.succeeded,
        failure_code=body.failure_code,
        failure_message=body.failure_message,
        processor=body.processor,
        reference=body.reference,
    )
    session.commit()
    session.refresh(attempt)
    session.refresh(invoice)
    return envelope(
        {
            **dunning.serialize(attempt),
            "invoice_state": invoice.state,
            "dunning_exhausted": dunning.exhausted(session, invoice),
        }
    )


@billing_router.get("/ledger")
def read_ledger(
    account: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The append-only ledger, newest first, with the balance each entry produced."""
    ensure_agent_can(session, agent_auth, READ)
    entries = ledger.entries_for(session, org_id=agent_auth.org.id, account=account, limit=limit)
    return envelope(
        {
            "entries": [ledger.serialize(entry) for entry in entries],
            "receivable": ledger.balance(
                session, org_id=agent_auth.org.id, account=ledger.ACCOUNT_RECEIVABLE
            ).as_dict(),
            "payable": ledger.balance(
                session, org_id=agent_auth.org.id, account=ledger.ACCOUNT_PAYABLE
            ).as_dict(),
            **ledger.describe(),
        }
    )


# ── Settlements ──────────────────────────────────────────────────────────────


@settlements_router.post("/run", status_code=201)
def run_settlement(
    body: SettlementRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Compute a provider's revenue share for a period.

    Defaults to the caller's own organization: a tenant settles what it earned,
    and running somebody else's settlement is not something this route offers.
    """
    ensure_agent_can(session, agent_auth, MANAGE)
    provider_org_id = body.provider_org_id or agent_auth.org.id
    if provider_org_id != agent_auth.org.id:
        raise InvalidRequestError(
            "A settlement is run for your own organization. Another tenant's earnings are not "
            "yours to compute."
        )
    record = settlement.run(
        session,
        provider_org_id=provider_org_id,
        period_start=body.period_start,
        period_end=body.period_end,
        provider_share_bps=body.provider_share_bps,
        fail_with=body.simulate_failure,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="billing.settlement_run",
        summary=(
            f"Settlement {record.state} at {record.provider_share_bps / 100:g}% to the provider"
        ),
        target_type="settlement",
        target_id=str(record.id),
        metadata={
            "state": record.state,
            "provider_share_bps": record.provider_share_bps,
            "provider_micros": record.provider_micros,
        },
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(record)
    return envelope(settlement.serialize(record))


@settlements_router.get("")
def list_settlements(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    rows = settlement.list_settlements(session, provider_org_id=agent_auth.org.id)
    return envelope(
        {
            "settlements": [settlement.serialize(row) for row in rows],
            **settlement.describe(),
        }
    )


@settlements_router.post("/{settlement_id}/retry")
def retry_settlement(
    settlement_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Resume a paused run — the same row, so the payable is not doubled."""
    ensure_agent_can(session, agent_auth, MANAGE)
    record = settlement.get(session, settlement_id, agent_auth.org.id)
    if record is None:
        raise NotFoundError(f"Settlement {settlement_id} was not found.")
    settlement.retry(session, record)
    session.commit()
    session.refresh(record)
    return envelope(settlement.serialize(record))


@settlements_router.post("/{settlement_id}/payout")
def record_payout(
    settlement_id: uuid.UUID,
    body: PayoutRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Record a payout made elsewhere. This platform does not move money."""
    ensure_agent_can(session, agent_auth, MANAGE)
    record = settlement.get(session, settlement_id, agent_auth.org.id)
    if record is None:
        raise NotFoundError(f"Settlement {settlement_id} was not found.")
    settlement.mark_paid_out(session, record, reference=body.reference)
    session.commit()
    session.refresh(record)
    return envelope(settlement.serialize(record))
