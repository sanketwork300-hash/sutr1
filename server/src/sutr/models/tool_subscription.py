import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# The LLD's subscription lifecycle (§3.7): Discover → Subscribe → Provision →
# Use → Renew → Cancel. Discover is not a state — it is what happens before a
# subscription exists — so the states begin at Subscribe.
STATE_SUBSCRIBED = "subscribed"
STATE_PROVISIONING = "provisioning"
STATE_ACTIVE = "active"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"
# Provisioning can fail, and a subscription stuck in `provisioning` forever
# would be a lie of omission.
STATE_FAILED = "failed"

STATES = (
    STATE_SUBSCRIBED,
    STATE_PROVISIONING,
    STATE_ACTIVE,
    STATE_CANCELLED,
    STATE_EXPIRED,
    STATE_FAILED,
)
# States from which a subscription can still become active. A tenant may hold
# only one of these per tool at a time.
OPEN_STATES = (STATE_SUBSCRIBED, STATE_PROVISIONING, STATE_ACTIVE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolSubscription(SQLModel, table=True):
    """One tenant's subscription to one registry tool.

    `org_id` is the **consumer**, not the provider — the two are different
    tenants in the case this table exists for, and confusing them would be a
    cross-tenant bug that reads like a naming choice. The provider is found
    through `tool_id`.

    The price is snapshotted by id at subscribe time. A provider raising their
    price must not silently reprice everyone who already subscribed, and
    pointing at the live price would do exactly that.

    Cancelled subscriptions are kept. "Who used to have access to this" is a
    question worth being able to answer, and re-subscribing writes a new row
    rather than reviving an old one.
    """

    __tablename__ = "tool_subscription"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    tool_id: uuid.UUID = Field(foreign_key="registry_tool.id", index=True)
    # Denormalized so a provider can list who subscribed to their tools without
    # reading the consumer's rows through a join across a tenant boundary.
    provider_org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    state: str = Field(default=STATE_SUBSCRIBED, index=True)
    # The price this subscription was sold at, not the price it would be sold
    # at today.
    pricing_id: uuid.UUID | None = Field(default=None, foreign_key="registry_pricing.id")
    # The tool version in force when it was provisioned.
    version: int | None = None

    subscribed_at: datetime = Field(default_factory=_utcnow)
    provisioned_at: datetime | None = None
    renews_at: datetime | None = None
    renewed_count: int = Field(default=0)
    cancelled_at: datetime | None = None
    cancel_reason: str = ""
    failure_reason: str = ""

    subscribed_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    updated_at: datetime = Field(default_factory=_utcnow)
