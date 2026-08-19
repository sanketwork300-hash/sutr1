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
    # Opaque per-kind extras (e.g. deployment id), JSON-encoded.
    metadata_json: str = Field(default="{}")
