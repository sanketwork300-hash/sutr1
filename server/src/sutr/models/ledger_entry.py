import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# The six entry kinds the LLD names (§5.1).
KIND_USAGE_CHARGE = "usage_charge"
KIND_CREDIT = "credit"
KIND_REFUND = "refund"
KIND_TAX = "tax"
KIND_SETTLEMENT = "settlement"
KIND_ADJUSTMENT = "adjustment"

KINDS = (
    KIND_USAGE_CHARGE,
    KIND_CREDIT,
    KIND_REFUND,
    KIND_TAX,
    KIND_SETTLEMENT,
    KIND_ADJUSTMENT,
)

# Which way the money moves. Every entry is one or the other; an entry that is
# neither is an entry nobody can add up.
DEBIT = "debit"
CREDIT = "credit"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerEntry(SQLModel, table=True):
    """One immutable financial fact (ESDS LLD §5.1).

    *"No financial data is ever deleted; corrections are compensating
    transactions."* So there is no update path and no delete path in
    `billing/ledger.py`: a mistake is fixed by writing its opposite, which
    leaves both the mistake and the correction visible. An edited ledger is a
    ledger nobody can reconcile.

    `balance_micros` is the running balance **after** this entry, computed when
    the entry is appended. Storing it costs a column and buys the property that
    matters: any entry can be shown with the balance it produced, without
    replaying the whole account.
    """

    __tablename__ = "ledger_entry"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Whose account this entry belongs to. A settlement writes two entries —
    # one on each side — so the pair is linked by `settlement_id`.
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    account: str = Field(default="receivable", index=True)

    kind: str = Field(index=True)
    direction: str = Field(index=True)
    currency: str = "USD"
    amount_micros: int = Field(default=0)
    balance_micros: int = Field(default=0)

    description: str = ""
    # What this entry is about, for tracing back to the fact that caused it.
    usage_event_id: int | None = Field(default=None, foreign_key="usage_event.id")
    invoice_id: uuid.UUID | None = Field(default=None, foreign_key="invoice.id", index=True)
    settlement_id: uuid.UUID | None = Field(default=None, foreign_key="settlement.id", index=True)
    # Set on a compensating entry: which entry this one corrects.
    reverses_entry_id: uuid.UUID | None = Field(default=None, foreign_key="ledger_entry.id")

    metadata_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_utcnow, index=True)
