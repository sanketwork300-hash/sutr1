import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

STATE_PUBLISHED = "published"
STATE_DELISTED = "delisted"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketplaceListing(SQLModel, table=True):
    """The storefront's own copy of a published tool (ESDS LLD §3.7).

    *"Marketplace = read-optimized storefront derived from Registry events."*
    This table is that derivation, and it is a **projection**: every column is
    written by an event handler in `marketplace/projection.py` and by nothing
    else. Reading the registry directly from a listing endpoint would be
    simpler and would also make the two services one service with two names.

    Being a projection has a consequence worth being explicit about: it can lag.
    `source_event_id` and `projected_at` record which event produced the row, so
    "the listing is stale" is a thing a reader can see rather than suspect. A
    listing is never treated as authoritative — an install or a subscription
    re-reads the registry.
    """

    __tablename__ = "marketplace_listing"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    tool_id: uuid.UUID = Field(foreign_key="registry_tool.id", index=True, unique=True)
    # The provider tenant. Listings are cross-tenant by nature — that is what a
    # marketplace is — so this is display data, never an authorization input.
    provider_org_id: uuid.UUID = Field(foreign_key="org.id", index=True)

    tool_key: str = ""
    name: str = ""
    summary: str = ""
    description: str = ""
    category: str = Field(default="other", index=True)
    tags_json: str = Field(default="[]")
    regions_json: str = Field(default="[]")
    compliance_json: str = Field(default="[]")
    integration_id: str | None = Field(default=None, index=True)

    # Who may see the listing. Carried rather than resolved at query time
    # because the storefront query runs across tenants and must be able to
    # exclude another tenant's organization-visible tools in the WHERE clause,
    # not after the rows have been read.
    visibility: str = Field(default="public", index=True)
    # Carried so the storefront can show a deprecated tool as deprecated
    # rather than delisting it — consumers already using it need to see the
    # note, and a tool that vanishes tells them nothing.
    lifecycle_state: str = Field(default="PUBLISHED", index=True)
    deprecation_note: str = ""

    version: int | None = None
    versions_json: str = Field(default="[]")
    pricing_json: str = Field(default="null")
    provider_json: str = Field(default="{}")
    # The score and its explanation, computed when the listing was projected.
    # Null means "not computable", never zero — a tool nothing is known about
    # is not a tool known to be bad.
    trust_score: int | None = Field(default=None, index=True)
    trust_json: str = Field(default="{}")

    subscriber_count: int = Field(default=0)
    state: str = Field(default=STATE_PUBLISHED, index=True)
    published_at: datetime | None = None

    # Projection provenance.
    source_event_id: str = ""
    source_event_type: str = ""
    projected_at: datetime = Field(default_factory=_utcnow)
