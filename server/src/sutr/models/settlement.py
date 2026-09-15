import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# §5.1: *"Settlement fails — pause payout, preserve ledger, retry."* `PAUSED`
# is therefore a state and not an absence: a settlement that failed must be
# visible, retryable, and must not have taken the ledger with it.
STATE_PENDING = "pending"
STATE_COMPLETE = "complete"
STATE_PAUSED = "paused"

STATES = (STATE_PENDING, STATE_COMPLETE, STATE_PAUSED)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Settlement(SQLModel, table=True):
    """One revenue-share run for one provider over one period (LLD §5.1).

    The split is **never hard-coded** (build prompt §44). It comes from
    configuration, may be overridden per run, and is recorded on the row — so
    an old settlement is explained by the share that applied then, not by the
    share that applies now.

    Nothing here moves money. The row is the computed obligation and the ledger
    entries that record it; paying it out is a payment processor this platform
    does not wire for marketplace tools, and `describe()` says so.
    """

    __tablename__ = "settlement"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # The provider being settled to, and the platform tenant running it.
    provider_org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    period_start: datetime = Field(index=True)
    period_end: datetime = Field(index=True)
    state: str = Field(default=STATE_PENDING, index=True)

    currency: str = "USD"
    gross_micros: int = Field(default=0)
    provider_micros: int = Field(default=0)
    platform_micros: int = Field(default=0)
    # Basis points, recorded per run: 8000 is 80% to the provider.
    provider_share_bps: int = Field(default=8000)

    invoice_ids_json: str = Field(default="[]")
    failure_reason: str = ""
    attempts: int = Field(default=0)

    paid_out: bool = Field(default=False)
    payout_reference: str = ""
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class PaymentAttempt(SQLModel, table=True):
    """One attempt to collect an invoice, and the dunning that follows.

    §5.1: *"Payment fails — mark unpaid; dunning workflow."* Attempts are rows
    rather than a counter so the sequence — when, why it failed, when the next
    one is due — is legible instead of inferred.

    **This platform does not collect.** An attempt is recorded when something
    else reports one; nothing here charges a card for marketplace usage.
    """

    __tablename__ = "payment_attempt"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    invoice_id: uuid.UUID = Field(foreign_key="invoice.id", index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    attempt: int = Field(default=1)
    succeeded: bool = Field(default=False)
    failure_code: str = ""
    failure_message: str = ""
    # When the dunning workflow should try again. Null when it has given up or
    # when the attempt succeeded.
    next_attempt_at: datetime | None = Field(default=None, index=True)
    # Whose attempt this was: `stripe` for the platform plan, `external` for an
    # attempt reported from outside.
    processor: str = Field(default="external")
    reference: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
