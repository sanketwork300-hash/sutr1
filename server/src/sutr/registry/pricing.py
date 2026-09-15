"""Pricing as history rather than as a setting.

LLD §3.7 lists pricing as marketplace metadata and puts changes to it behind
governance. Two consequences shape this module.

**A price is never edited.** Changing it supersedes the live row and writes a
new one. A subscription points at the row it was sold under, so a provider
raising their price does not silently reprice everyone who already subscribed,
and an invoice from March stays explainable in June.

**Nothing here charges anybody.** This is metadata a billing service can later
read. The platform's own Stripe subscription is a different thing entirely and
is untouched — conflating the two would mean a provider editing a marketplace
price could move real money (ADR-046).
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import InvalidRequestError
from sutr.models.registry_pricing import (
    MODEL_CUSTOM,
    MODEL_FREE,
    MODEL_PER_CALL,
    MODEL_SUBSCRIPTION,
    MODELS,
    UNIT_CALL,
    UNIT_MONTH,
    RegistryPricing,
)

MICROS = 1_000_000
# A currency code is three letters by ISO 4217. Not validated against the list:
# the list changes, and refusing a real currency because this code has an old
# copy of it would be worse than accepting a typo.
_CURRENCY_LENGTH = 3


def validate(
    *, model: str, amount_micros: int, unit: str, currency: str, free_allowance: int
) -> None:
    if model not in MODELS:
        raise InvalidRequestError(f"Unknown pricing model '{model}'. Known: {', '.join(MODELS)}.")
    if amount_micros < 0 or free_allowance < 0:
        raise InvalidRequestError("A price and a free allowance cannot be negative.")
    if model == MODEL_FREE and amount_micros:
        raise InvalidRequestError(
            "A free plan cannot carry an amount. Use per_call or subscription."
        )
    if model in (MODEL_PER_CALL, MODEL_SUBSCRIPTION) and not amount_micros:
        raise InvalidRequestError(
            f"A {model} plan needs an amount. A zero price is the `free` model, which says so."
        )
    if model == MODEL_PER_CALL and unit != UNIT_CALL:
        raise InvalidRequestError("A per_call plan is priced per call.")
    if model == MODEL_SUBSCRIPTION and unit != UNIT_MONTH:
        raise InvalidRequestError("A subscription plan is priced per month.")
    if len(currency) != _CURRENCY_LENGTH or not currency.isalpha():
        raise InvalidRequestError("A currency is a three-letter ISO 4217 code, such as USD.")


def current(session: Session, tool_id: uuid.UUID) -> RegistryPricing | None:
    """The live price: the one row that has not been superseded."""
    return session.exec(
        select(RegistryPricing)
        .where(RegistryPricing.tool_id == tool_id)
        .where(col(RegistryPricing.superseded_at).is_(None))
        .order_by(desc(col(RegistryPricing.effective_from)))
    ).first()


def history(session: Session, tool_id: uuid.UUID) -> list[RegistryPricing]:
    return list(
        session.exec(
            select(RegistryPricing)
            .where(RegistryPricing.tool_id == tool_id)
            .order_by(desc(col(RegistryPricing.effective_from)))
        ).all()
    )


def set_price(
    session: Session,
    *,
    tool_id: uuid.UUID,
    org_id: uuid.UUID,
    model: str,
    amount_micros: int = 0,
    unit: str = UNIT_CALL,
    currency: str = "USD",
    free_allowance: int = 0,
    notes: str = "",
    created_by_user_id: uuid.UUID | None = None,
) -> RegistryPricing:
    """Supersede the live price and write the new one. The caller commits."""
    currency = currency.upper()
    validate(
        model=model,
        amount_micros=amount_micros,
        unit=unit,
        currency=currency,
        free_allowance=free_allowance,
    )
    now = datetime.now(timezone.utc)
    live = current(session, tool_id)
    if live is not None:
        live.superseded_at = now
        session.add(live)
    price = RegistryPricing(
        tool_id=tool_id,
        org_id=org_id,
        model=model,
        currency=currency,
        amount_micros=amount_micros,
        unit=unit,
        free_allowance=free_allowance,
        notes=notes,
        effective_from=now,
        created_by_user_id=created_by_user_id,
    )
    session.add(price)
    session.flush()
    return price


def display(price: RegistryPricing | None) -> str:
    """A price a person can read, without a currency library."""
    if price is None:
        return "not priced"
    if price.model == MODEL_FREE:
        return "free"
    if price.model == MODEL_CUSTOM:
        return "by arrangement with the provider"
    amount = price.amount_micros / MICROS
    per = "call" if price.unit == UNIT_CALL else "month"
    text = f"{amount:.6f}".rstrip("0").rstrip(".")
    line = f"{text} {price.currency} per {per}"
    if price.free_allowance:
        line += f", first {price.free_allowance} calls free"
    return line


def serialize(price: RegistryPricing | None) -> dict[str, Any] | None:
    """Null for a tool nobody has priced — not a free plan.

    "Nobody set a price" and "the provider chose to charge nothing" are
    different facts, and rendering the first as the second would put a `free`
    badge on tools whose provider never made that decision.
    """
    if price is None:
        return None
    return {
        "id": str(price.id),
        "model": price.model,
        "currency": price.currency,
        "amount_micros": price.amount_micros,
        "unit": price.unit,
        "free_allowance": price.free_allowance,
        "notes": price.notes,
        "display": display(price),
        "effective_from": price.effective_from.isoformat(),
        "superseded_at": price.superseded_at.isoformat() if price.superseded_at else None,
        "billed_by_this_platform": False,
    }
