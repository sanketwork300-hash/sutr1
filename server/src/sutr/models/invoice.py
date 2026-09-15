import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

# An invoice's life. `FAILED` is its own state because §5.1 says invoice
# generation must *"async retry — never block metering"*: a generation that
# failed has to be visible and retryable rather than absent.
STATE_DRAFT = "draft"
STATE_ISSUED = "issued"
STATE_PAID = "paid"
STATE_UNPAID = "unpaid"
STATE_VOID = "void"
STATE_FAILED = "failed"

STATES = (STATE_DRAFT, STATE_ISSUED, STATE_PAID, STATE_UNPAID, STATE_VOID, STATE_FAILED)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Invoice(SQLModel, table=True):
    """One billing period's charges for one tenant (ESDS LLD §5.1).

    Generated **after** the fact, from usage that was metered without any price
    attached. An invoice is therefore reproducible: the same usage and the same
    published plans produce the same invoice, and `plan_versions_json` records
    which plan versions were used so a disputed line can be explained.

    Never deleted. A wrong invoice is voided and a new one issued, and both
    remain — §5.1: *"corrections are compensating transactions."*
    """

    __tablename__ = "invoice"
    __table_args__ = (
        # One invoice per tenant per period. A second one would double-bill the
        # same usage, which is the failure mode this table exists to avoid.
        UniqueConstraint("org_id", "period_start", "period_end", name="uq_invoice_period"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    number: str = Field(default="", index=True)

    period_start: datetime = Field(index=True)
    period_end: datetime = Field(index=True)

    state: str = Field(default=STATE_DRAFT, index=True)
    currency: str = "USD"
    subtotal_micros: int = Field(default=0)
    discount_micros: int = Field(default=0)
    tax_micros: int = Field(default=0)
    total_micros: int = Field(default=0)

    # Which plan versions priced this invoice, so a line can be explained.
    plan_versions_json: str = Field(default="[]")
    # Set when generation failed, so a retry knows what went wrong.
    failure_reason: str = ""
    attempts: int = Field(default=0)

    issued_at: datetime | None = None
    paid_at: datetime | None = None
    voided_at: datetime | None = None
    void_reason: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class InvoiceLine(SQLModel, table=True):
    """One priced group of usage on an invoice.

    Lines carry the **pricing context**: which plan, which version, which
    model, how many units and how the amount was reached. A line nobody can
    explain is a line somebody will dispute and nobody can defend.
    """

    __tablename__ = "invoice_line"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    invoice_id: uuid.UUID = Field(foreign_key="invoice.id", index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    description: str = ""
    usage_kind: str = ""
    integration_id: str | None = None
    tool_id: uuid.UUID | None = Field(default=None, foreign_key="registry_tool.id")

    quantity: int = Field(default=0)
    plan_id: uuid.UUID | None = Field(default=None, foreign_key="pricing_plan.id")
    plan_key: str = ""
    plan_version: int = 0
    plan_model: str = ""

    subtotal_micros: int = Field(default=0)
    discount_micros: int = Field(default=0)
    tax_micros: int = Field(default=0)
    total_micros: int = Field(default=0)
    # The arithmetic, step by step: usage → plan → discount → tax → charge.
    breakdown_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_utcnow)
