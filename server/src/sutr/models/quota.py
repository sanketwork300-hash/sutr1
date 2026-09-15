import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# What is being limited. Each kind names both the metric and its window, so a
# limit row needs no separate "period" column and cannot be ambiguous.
DAILY_TOOL_CALLS = "daily_tool_calls"
MONTHLY_TOOL_CALLS = "monthly_tool_calls"
CONCURRENT_TOOL_CALLS = "concurrent_tool_calls"
DAILY_TOKENS = "daily_tokens"
MONTHLY_TOKENS = "monthly_tokens"
DAILY_DATA_TRANSFER_BYTES = "daily_data_transfer_bytes"
MONTHLY_DATA_TRANSFER_BYTES = "monthly_data_transfer_bytes"

QUOTA_KINDS = (
    DAILY_TOOL_CALLS,
    MONTHLY_TOOL_CALLS,
    CONCURRENT_TOOL_CALLS,
    DAILY_TOKENS,
    MONTHLY_TOKENS,
    DAILY_DATA_TRANSFER_BYTES,
    MONTHLY_DATA_TRANSFER_BYTES,
)

# What the limit applies to.
SCOPE_TENANT = "tenant"  # everything the org does
SCOPE_INTEGRATION = "integration"  # one integration
SCOPE_TOOL = "tool"  # one tool of one integration

SCOPES = (SCOPE_TENANT, SCOPE_INTEGRATION, SCOPE_TOOL)


class Quota(SQLModel, table=True):
    """One usage limit, checked before a tool executes.

    ESDS LLD §5.1 requires quotas to be evaluated *before* execution, and build
    prompt §48 adds that a rejected request must not be billed as a successful
    one. Both properties live in `services/quota.py`, which this row feeds.

    A quota is a limit, not a counter: the count is derived from the usage
    ledger (`usage_event`), which is already the durable, authoritative record
    of what happened. Keeping a second counter would introduce a second truth
    that could disagree with the bill.
    """

    __tablename__ = "quota"
    __table_args__ = (
        UniqueConstraint("org_id", "kind", "scope", "scope_id", name="uq_quota_scope"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    kind: str
    scope: str = SCOPE_TENANT
    # "" for tenant scope; the integration id, or "<integration>/<tool>", otherwise.
    scope_id: str = ""
    limit_value: int = 0
    # A disabled quota is kept rather than deleted so the limit it used to
    # impose is still visible, and so re-enabling it does not lose the number.
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
