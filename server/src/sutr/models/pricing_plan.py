import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

# The eight pricing models the LLD names (§5.1). Every one of them is a way of
# turning a quantity into an amount; `pricing.py` holds one function per model
# and refuses a plan whose model it cannot evaluate.
MODEL_FREE = "free"
MODEL_PER_INVOCATION = "per_invocation"
MODEL_PER_API_CALL = "per_api_call"
MODEL_PER_SECOND = "per_second"
MODEL_SUBSCRIPTION = "subscription"
MODEL_TIERED = "tiered"
MODEL_HYBRID = "hybrid"
MODEL_ENTERPRISE = "enterprise"

MODELS = (
    MODEL_FREE,
    MODEL_PER_INVOCATION,
    MODEL_PER_API_CALL,
    MODEL_PER_SECOND,
    MODEL_SUBSCRIPTION,
    MODEL_TIERED,
    MODEL_HYBRID,
    MODEL_ENTERPRISE,
)

# A plan is published before it can price anything, and superseded rather than
# edited — §5.1's *"Pricing rule unavailable — use latest published version"*
# only means something if versions are a thing that exists.
STATE_DRAFT = "draft"
STATE_PUBLISHED = "published"
STATE_SUPERSEDED = "superseded"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PricingPlan(SQLModel, table=True):
    """A published rule for turning usage into money (ESDS LLD §5.1).

    Distinct from `registry_pricing`, and the distinction is the point.
    `registry_pricing` is what a provider **advertises** in the marketplace; a
    plan is what **bills**. They are usually the same number and they are not
    the same fact: a subscription snapshots the advertised price it was sold
    at, while an invoice is computed from the plan that was published when the
    usage happened.

    Nothing here is evaluated during execution. LLD §5.1: *"prices are
    evaluated at billing time, never during execution"*, and the way that is
    kept true is that the metering path cannot reach this table — `record_usage`
    takes no amount and writes none.
    """

    __tablename__ = "pricing_plan"
    __table_args__ = (UniqueConstraint("org_id", "key", "version", name="uq_pricing_plan_version"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    key: str = Field(index=True)
    name: str = ""
    version: int = Field(default=1)
    state: str = Field(default=STATE_DRAFT, index=True)

    model: str = Field(default=MODEL_FREE, index=True)
    currency: str = "USD"
    # Integer micro-units. A price held as a float is a price that is eventually
    # off by a hundredth somewhere it matters.
    amount_micros: int = Field(default=0)
    # Units included before `amount_micros` starts applying.
    included_units: int = Field(default=0)
    # For subscription and hybrid: the recurring part, charged per period.
    base_micros: int = Field(default=0)
    # For tiered and hybrid: [{"up_to": 1000, "amount_micros": 250}, ...] with a
    # final entry whose `up_to` is null meaning "everything above".
    tiers_json: str = Field(default="[]")

    # What this plan prices. Null scopes it to the whole tenant.
    tool_id: uuid.UUID | None = Field(default=None, foreign_key="registry_tool.id", index=True)
    # Which metered kind it applies to (`tool_call`, `deployment_runtime`).
    usage_kind: str = Field(default="tool_call", index=True)

    # Percentages in basis points: 250 is 2.5%. Integers, for the same reason
    # amounts are.
    discount_bps: int = Field(default=0)
    tax_bps: int = Field(default=0)
    tax_label: str = ""
    notes: str = ""

    published_at: datetime | None = None
    superseded_at: datetime | None = None
    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
