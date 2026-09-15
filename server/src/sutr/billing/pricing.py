"""Turning usage into money, at billing time and never before.

LLD §5.1 states the pipeline and the constraint in one breath:

    Pricing evaluation: Usage Event → Pricing Plan → Discount → Tax → Charge
    ... prices are evaluated at billing time, never during execution.

The constraint is structural here, not a convention. `record_usage` takes no
amount and writes none; nothing on the execution path can reach this module.
A metered event is a *fact about what happened*, and the price of that fact is
decided later by whichever plan was published at the time — which is what makes
a repriced tool not retroactively repriced.

Eight models, because the LLD names eight. Each is a function from a quantity to
an amount, and a plan whose model has no function is refused when it is
published rather than when an invoice is generated: an unpriceable plan
discovered at billing time is an invoice that cannot be produced.

Money is integer micro-units throughout — 1_000_000 is one unit of currency.
Percentages are basis points. A price held as a float is a price that is
eventually off by a hundredth somewhere it matters, and rounding is applied once
at the end of each step rather than accumulated.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.models.pricing_plan import (
    MODEL_ENTERPRISE,
    MODEL_FREE,
    MODEL_HYBRID,
    MODEL_PER_API_CALL,
    MODEL_PER_INVOCATION,
    MODEL_PER_SECOND,
    MODEL_SUBSCRIPTION,
    MODEL_TIERED,
    MODELS,
    STATE_DRAFT,
    STATE_PUBLISHED,
    STATE_SUPERSEDED,
    PricingPlan,
)

MICROS = 1_000_000
BPS = 10_000

# Models whose amount is not derived from a quantity at all. Named so the
# evaluator can say *why* it produced what it did rather than returning zero
# with no explanation.
FLAT_MODELS = (MODEL_FREE, MODEL_SUBSCRIPTION, MODEL_ENTERPRISE)


class PricingError(Exception):
    """A plan could not be evaluated. The message names the plan and the reason."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Charge:
    """The result of the LLD's pipeline, with every step kept.

    A charge nobody can explain is a charge somebody will dispute and nobody
    can defend, so each stage's number survives into the invoice line.
    """

    quantity: int
    subtotal_micros: int
    discount_micros: int
    taxable_micros: int
    tax_micros: int
    total_micros: int
    currency: str
    model: str
    steps: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "quantity": self.quantity,
            "subtotal_micros": self.subtotal_micros,
            "discount_micros": self.discount_micros,
            "taxable_micros": self.taxable_micros,
            "tax_micros": self.tax_micros,
            "total_micros": self.total_micros,
            "currency": self.currency,
            "model": self.model,
            # Usage → Plan → Discount → Tax → Charge, in words.
            "steps": self.steps,
            "detail": self.detail,
        }


# ── The eight models ─────────────────────────────────────────────────────────


def _billable(quantity: int, included: int) -> int:
    return max(0, quantity - max(0, included))


def _per_unit(plan: PricingPlan, quantity: int) -> tuple[int, dict[str, Any]]:
    billable = _billable(quantity, plan.included_units)
    return billable * plan.amount_micros, {
        "billable_units": billable,
        "included_units": plan.included_units,
        "unit_micros": plan.amount_micros,
    }


def _tiers(plan: PricingPlan) -> list[dict[str, Any]]:
    return json.loads(plan.tiers_json or "[]")


def _tiered(plan: PricingPlan, quantity: int) -> tuple[int, dict[str, Any]]:
    """Graduated tiers: each band is charged at its own rate.

    Graduated rather than volume pricing — the first thousand calls stay at the
    first thousand's rate when the ten-thousandth is made. Volume pricing (the
    whole quantity at the rate its total reaches) is a different product
    decision, and picking one silently would make an invoice inexplicable.
    """
    billable = _billable(quantity, plan.included_units)
    remaining = billable
    total = 0
    used: list[dict[str, Any]] = []
    consumed = 0
    for tier in _tiers(plan):
        if remaining <= 0:
            break
        up_to = tier.get("up_to")
        rate = int(tier.get("amount_micros", 0))
        capacity = remaining if up_to is None else max(0, int(up_to) - consumed)
        take = min(remaining, capacity)
        if take <= 0:
            continue
        total += take * rate
        used.append({"up_to": up_to, "units": take, "unit_micros": rate})
        remaining -= take
        consumed += take
    if remaining > 0:
        raise PricingError(
            f"Plan '{plan.key}' v{plan.version} has {remaining} unit(s) above its highest tier. "
            "A tiered plan needs a final tier with `up_to: null` covering everything above."
        )
    return total, {"tiers_used": used, "billable_units": billable}


EVALUATORS = {
    MODEL_FREE: lambda plan, quantity: (0, {"reason": "free plan"}),
    MODEL_PER_INVOCATION: _per_unit,
    MODEL_PER_API_CALL: _per_unit,
    MODEL_PER_SECOND: lambda plan, quantity: _per_unit(plan, quantity),
    MODEL_SUBSCRIPTION: lambda plan, quantity: (
        plan.base_micros,
        {"reason": "recurring base charge", "base_micros": plan.base_micros},
    ),
    MODEL_TIERED: _tiered,
    MODEL_ENTERPRISE: lambda plan, quantity: (
        plan.base_micros,
        {
            "reason": "negotiated flat amount",
            "base_micros": plan.base_micros,
            "note": (
                "An enterprise plan is a number somebody agreed off-platform. Nothing here "
                "derives it from usage."
            ),
        },
    ),
}


def _hybrid(plan: PricingPlan, quantity: int) -> tuple[int, dict[str, Any]]:
    """A recurring base plus usage. Tiers when present, flat rate otherwise."""
    usage, detail = _tiered(plan, quantity) if _tiers(plan) else _per_unit(plan, quantity)
    return plan.base_micros + usage, {
        **detail,
        "base_micros": plan.base_micros,
        "usage_micros": usage,
    }


EVALUATORS[MODEL_HYBRID] = _hybrid


def validate(plan: PricingPlan) -> None:
    """Refuse a plan that could not be evaluated, at publish time.

    Publishing is where a pricing mistake is cheap. An unpriceable plan
    discovered during invoice generation is an invoice that cannot be produced
    for a period that has already happened.
    """
    if plan.model not in MODELS:
        raise InvalidRequestError(
            f"Unknown pricing model '{plan.model}'. Known: {', '.join(MODELS)}."
        )
    if plan.model not in EVALUATORS:
        raise InvalidRequestError(f"No evaluator is implemented for '{plan.model}'.")
    if plan.amount_micros < 0 or plan.base_micros < 0 or plan.included_units < 0:
        raise InvalidRequestError("A price, base amount or included allowance cannot be negative.")
    if not (0 <= plan.discount_bps <= BPS) or not (0 <= plan.tax_bps <= BPS):
        raise InvalidRequestError("A discount or tax rate is between 0 and 10000 basis points.")
    if plan.model in (MODEL_PER_INVOCATION, MODEL_PER_API_CALL, MODEL_PER_SECOND):
        if not plan.amount_micros:
            raise InvalidRequestError(
                f"A {plan.model} plan needs a unit amount. A zero rate is the `free` model, "
                "which says so."
            )
    if plan.model in (MODEL_SUBSCRIPTION, MODEL_ENTERPRISE) and not plan.base_micros:
        raise InvalidRequestError(f"A {plan.model} plan needs a base amount.")
    if plan.model in (MODEL_TIERED, MODEL_HYBRID):
        tiers = _tiers(plan)
        if plan.model == MODEL_TIERED and not tiers:
            raise InvalidRequestError("A tiered plan needs tiers.")
        if tiers:
            bounds = [tier.get("up_to") for tier in tiers]
            if bounds and bounds[-1] is not None:
                raise InvalidRequestError(
                    "A tiered plan's last tier needs `up_to: null` so every quantity is priced. "
                    "Without it a large enough invoice cannot be produced at all."
                )
            numbered = [b for b in bounds if b is not None]
            if numbered != sorted(numbered):
                raise InvalidRequestError("Tier bounds must ascend.")


def evaluate(plan: PricingPlan, quantity: int) -> Charge:
    """Usage → Plan → Discount → Tax → Charge, with each step recorded."""
    evaluator = EVALUATORS.get(plan.model)
    if evaluator is None:
        raise PricingError(f"No evaluator for pricing model '{plan.model}'.")
    subtotal, detail = evaluator(plan, quantity)

    discount = round(subtotal * plan.discount_bps / BPS)
    taxable = subtotal - discount
    tax = round(taxable * plan.tax_bps / BPS)
    total = taxable + tax

    steps = [
        f"usage: {quantity} unit(s) of {plan.usage_kind}",
        f"plan: {plan.key} v{plan.version} ({plan.model}) → {_money(subtotal, plan.currency)}",
    ]
    if plan.discount_bps:
        steps.append(f"discount: {plan.discount_bps / 100:g}% → −{_money(discount, plan.currency)}")
    if plan.tax_bps:
        label = plan.tax_label or "tax"
        steps.append(f"{label}: {plan.tax_bps / 100:g}% → +{_money(tax, plan.currency)}")
    steps.append(f"charge: {_money(total, plan.currency)}")

    return Charge(
        quantity=quantity,
        subtotal_micros=subtotal,
        discount_micros=discount,
        taxable_micros=taxable,
        tax_micros=tax,
        total_micros=total,
        currency=plan.currency,
        model=plan.model,
        steps=steps,
        detail=detail,
    )


def _money(micros: int, currency: str) -> str:
    return f"{micros / MICROS:.6f}".rstrip("0").rstrip(".") + f" {currency}"


# ── Plans ────────────────────────────────────────────────────────────────────


def create(
    session: Session,
    *,
    org_id: uuid.UUID,
    key: str,
    name: str,
    model: str,
    usage_kind: str = "tool_call",
    tool_id: uuid.UUID | None = None,
    currency: str = "USD",
    amount_micros: int = 0,
    included_units: int = 0,
    base_micros: int = 0,
    tiers: list[dict[str, Any]] | None = None,
    discount_bps: int = 0,
    tax_bps: int = 0,
    tax_label: str = "",
    notes: str = "",
    created_by_user_id: uuid.UUID | None = None,
) -> PricingPlan:
    """Draft the next version of a plan. The caller commits."""
    from sqlalchemy import func

    highest = session.exec(
        select(func.max(PricingPlan.version))
        .where(PricingPlan.org_id == org_id)
        .where(PricingPlan.key == key)
    ).one()
    plan = PricingPlan(
        org_id=org_id,
        key=key,
        name=name,
        version=int(highest or 0) + 1,
        state=STATE_DRAFT,
        model=model,
        usage_kind=usage_kind,
        tool_id=tool_id,
        currency=currency.upper(),
        amount_micros=amount_micros,
        included_units=included_units,
        base_micros=base_micros,
        tiers_json=json.dumps(tiers or []),
        discount_bps=discount_bps,
        tax_bps=tax_bps,
        tax_label=tax_label,
        notes=notes,
        created_by_user_id=created_by_user_id,
    )
    validate(plan)
    session.add(plan)
    session.flush()
    return plan


def publish(session: Session, plan: PricingPlan) -> PricingPlan:
    """Publish a plan, superseding the previously published version of its key."""
    if plan.state != STATE_DRAFT:
        raise ConflictError(f"This plan is already {plan.state}.")
    validate(plan)
    now = _utcnow()
    live = session.exec(
        select(PricingPlan)
        .where(PricingPlan.org_id == plan.org_id)
        .where(PricingPlan.key == plan.key)
        .where(PricingPlan.state == STATE_PUBLISHED)
    ).all()
    for previous in live:
        previous.state = STATE_SUPERSEDED
        previous.superseded_at = now
        session.add(previous)
    plan.state = STATE_PUBLISHED
    plan.published_at = now
    session.add(plan)
    return plan


def published_for(
    session: Session,
    *,
    org_id: uuid.UUID,
    usage_kind: str,
    tool_id: uuid.UUID | None = None,
) -> PricingPlan | None:
    """The plan that prices this usage.

    A tool-specific plan wins over a tenant-wide one — the specific rule is the
    one somebody wrote on purpose. Among equals the **latest published version**
    wins, which is also §5.1's answer to *"pricing rule unavailable — use latest
    published version"*.
    """
    statement = (
        select(PricingPlan)
        .where(PricingPlan.org_id == org_id)
        .where(PricingPlan.state == STATE_PUBLISHED)
        .where(PricingPlan.usage_kind == usage_kind)
    )
    candidates = list(session.exec(statement).all())
    if not candidates:
        return None
    specific = [plan for plan in candidates if tool_id and plan.tool_id == tool_id]
    general = [plan for plan in candidates if plan.tool_id is None]
    pool = specific or general
    if not pool:
        return None
    return max(pool, key=lambda plan: (plan.published_at or plan.created_at, plan.version))


def list_plans(session: Session, *, org_id: uuid.UUID) -> list[PricingPlan]:
    return list(
        session.exec(
            select(PricingPlan)
            .where(PricingPlan.org_id == org_id)
            .order_by(col(PricingPlan.key), desc(col(PricingPlan.version)))
        ).all()
    )


def get(session: Session, plan_id: uuid.UUID, org_id: uuid.UUID) -> PricingPlan | None:
    plan = session.get(PricingPlan, plan_id)
    if plan is None or plan.org_id != org_id:
        return None
    return plan


def serialize(plan: PricingPlan) -> dict[str, Any]:
    return {
        "id": str(plan.id),
        "key": plan.key,
        "name": plan.name,
        "version": plan.version,
        "state": plan.state,
        "model": plan.model,
        "usage_kind": plan.usage_kind,
        "tool_id": str(plan.tool_id) if plan.tool_id else None,
        "currency": plan.currency,
        "amount_micros": plan.amount_micros,
        "included_units": plan.included_units,
        "base_micros": plan.base_micros,
        "tiers": _tiers(plan),
        "discount_bps": plan.discount_bps,
        "tax_bps": plan.tax_bps,
        "tax_label": plan.tax_label or None,
        "notes": plan.notes or None,
        "published_at": plan.published_at.isoformat() if plan.published_at else None,
        "superseded_at": plan.superseded_at.isoformat() if plan.superseded_at else None,
        "created_at": plan.created_at.isoformat(),
    }


def describe() -> dict[str, Any]:
    return {
        "models": list(MODELS),
        "implemented": sorted(EVALUATORS),
        "pipeline": ["usage_event", "pricing_plan", "discount", "tax", "charge"],
        "money": "integer micro-units; 1000000 is one unit of currency",
        "rates": "basis points; 250 is 2.5%",
        "evaluated_at": "billing time",
        "note": (
            "Nothing on the execution path can reach a plan: `record_usage` takes no amount and "
            "writes none, so a repriced tool is not retroactively repriced."
        ),
        "tiering": "graduated — each band is charged at its own rate, not the whole quantity",
    }
