import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# Pricing models. Deliberately few: the LLD names pricing as marketplace
# metadata, not as a billing engine, and inventing tiers and proration here
# would be inventing a product nobody specified.
MODEL_FREE = "free"
MODEL_PER_CALL = "per_call"
MODEL_SUBSCRIPTION = "subscription"
# The provider prices this off-platform. Named rather than left blank, because
# "no pricing row" and "priced by arrangement" are different facts.
MODEL_CUSTOM = "custom"
MODELS = (MODEL_FREE, MODEL_PER_CALL, MODEL_SUBSCRIPTION, MODEL_CUSTOM)

UNIT_CALL = "call"
UNIT_MONTH = "month"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryPricing(SQLModel, table=True):
    """One priced offer for a registry tool, immutable once written.

    Prices are **history, not settings**. A row is never edited: changing the
    price supersedes the current row and writes a new one, so a subscription
    can point at the price it was actually sold at and an invoice from March
    can be explained in June.

    Amounts are integer micro-units of the currency — 1_000_000 is one unit —
    because a price stored as a float is a price that will eventually be off by
    a hundredth somewhere it matters.

    **Nothing here charges anybody.** This is marketplace metadata that billing
    can later read; the platform's own Stripe subscription is a separate thing
    and is untouched (ADR-046).
    """

    __tablename__ = "registry_pricing"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    tool_id: uuid.UUID = Field(foreign_key="registry_tool.id", index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    model: str = Field(default=MODEL_FREE)
    currency: str = "USD"
    amount_micros: int = 0
    unit: str = UNIT_CALL
    # Calls included before `amount_micros` starts applying, for per-call plans.
    free_allowance: int = 0
    notes: str = ""

    effective_from: datetime = Field(default_factory=_utcnow)
    # Null while this is the live price. Set when a newer row replaces it.
    superseded_at: datetime | None = Field(default=None, index=True)

    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
