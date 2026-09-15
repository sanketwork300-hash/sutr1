import uuid
from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from sutr.observability.propagation import current_trace_id
from sutr.request_context import get_correlation_id


class LogEntry(SQLModel, table=True):
    __tablename__ = "log_entry"
    # Composite indexes matching the /api/logs filters (always org-scoped).
    __table_args__ = (
        Index("ix_log_entry_org_integration", "org_id", "integration_id"),
        Index("ix_log_entry_org_tool", "org_id", "tool_name"),
        Index("ix_log_entry_org_outcome", "org_id", "outcome"),
        Index("ix_log_entry_org_timestamp", "org_id", "timestamp"),
        Index("ix_log_entry_org_correlation", "org_id", "correlation_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    integration_id: str
    tool_name: str
    args_json: str | None = None
    result_json: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None  # pending | approved | executed | denied | error
    approval_request_id: uuid.UUID | None = None
    args_hash: str | None = None
    requester_ip: str | None = None
    user_agent: str | None = None
    api_key_label: str | None = None
    api_key_prefix: str | None = None
    access_reason: str | None = (
        None  # approved_once | approved_exact | approved_any | None (auto-allowed)
    )
    # Set when the call was made via admin impersonation — records the admin
    # who initiated the session so attribution can be separated from the
    # target user in the logs UI and downstream analytics.
    impersonator_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id", index=True)
    additional_info: str | None = None
    # Which logical operation this call belonged to, and — when tracing was
    # running — the trace that recorded it (LLD §5.3). The correlation id is
    # what joins the several rows of one operation together; the trace id is
    # what an operator pastes into a trace backend. Null where unknown: a call
    # made with tracing off has no trace, and saying otherwise would send a
    # reader looking for one.
    #
    # Filled from the ambient context rather than by the caller. Every call
    # site that writes a log entry is already inside the operation whose ids
    # these are; asking each of them to pass the ids along would only create
    # one more thing to forget.
    correlation_id: str | None = Field(default_factory=get_correlation_id)
    trace_id: str | None = Field(default_factory=current_trace_id)
