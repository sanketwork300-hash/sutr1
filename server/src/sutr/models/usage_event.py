import uuid
from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

# Metered event kinds.
KIND_TOOL_CALL = "tool_call"  # one billable unit per upstream execution attempt
KIND_DEPLOYMENT_RUNTIME = "deployment_runtime"  # quantity = minutes a deployment ran


class UsageEvent(SQLModel, table=True):
    """Durable metering ledger — the authoritative source for usage and billing.

    Distinct from LogEntry (operational detail, redacted, prunable) and
    AuditEvent (who changed what). Usage events carry no arguments or results,
    only counts and dimensions, so they are safe to keep forever — and they
    ARE kept forever: retention never prunes this table, because deleting it
    would destroy billing history.
    """

    __tablename__ = "usage_event"
    __table_args__ = (
        Index("ix_usage_event_org_ts", "org_id", "timestamp"),
        Index("ix_usage_event_org_kind_ts", "org_id", "kind", "timestamp"),
        # The dedupe LLD §5.1 asks for. NULLs do not collide, so an event whose
        # caller had no invocation id is still recorded; one that repeats an id
        # already seen for this tenant cannot become a second charge.
        Index("uq_usage_event_invocation", "org_id", "invocation_id", unique=True),
    )

    id: int | None = Field(default=None, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    kind: str
    # Billable magnitude: 1 per tool call, N minutes for deployment runtime.
    quantity: int = Field(default=1)
    # Breakdown dimensions (nullable — not every kind has every dimension).
    integration_id: str | None = None
    tool_name: str | None = None
    source: str | None = None  # "api" | "mcp" | "system"
    outcome: str | None = None  # "executed" | "error"
    duration_ms: int | None = None
    # Attribution: which human or key drove the usage.
    user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    api_key_prefix: str | None = None

    # ── The dimensions LLD §5.1.2 names, added in Phase 10 ───────────────────
    #
    # `invocation_id` is the dedupe key. §5.1 asks for *"dedupe via invocation
    # ID + idempotency keys"*, and a unique index on (org_id, invocation_id) is
    # what makes a replayed event a no-op rather than a double charge. Null
    # where the caller has no id to give: NULLs do not collide, so an event
    # without one is still recorded.
    invocation_id: str | None = Field(default=None, index=True)
    region: str | None = None
    payload_bytes: int | None = None
    tokens: int | None = None
    # Which provider earns from this usage, for revenue share.
    provider_org_id: uuid.UUID | None = Field(default=None, foreign_key="org.id", index=True)
    tool_id: uuid.UUID | None = Field(default=None, foreign_key="registry_tool.id", index=True)
    # Dimensions billing will need that are *not* a price: the plan is chosen
    # at billing time, so nothing here names one. LLD §5.1: prices are
    # evaluated at billing time, never during execution.
    pricing_context_json: str = Field(default="{}")
    # Opaque per-kind extras (e.g. deployment id), JSON-encoded.
    metadata_json: str = Field(default="{}")
