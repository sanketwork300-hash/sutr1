import uuid
from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class AuditEvent(SQLModel, table=True):
    """Control-plane audit trail: who changed what, when, from where.

    Tool executions are already covered by LogEntry; this table covers the
    actions *around* them — logins, key management, policy changes, approval
    decisions, integration installs, membership changes, impersonation.
    Rows are append-only and exempt from log retention.
    """

    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_org_action", "org_id", "action"),)

    id: int | None = Field(default=None, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # dotted verb, e.g. "policy.mode_changed", "api_key.created", "auth.login"
    action: str
    # "user" | "api_key" | "system"
    actor_type: str = Field(default="user")
    actor_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    actor_api_key_prefix: str | None = None
    # Set when the actor was an admin impersonating a user.
    impersonator_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    # What the action applied to, e.g. ("integration", "posthog"),
    # ("approval_request", "<uuid>"), ("user", "<uuid>").
    target_type: str | None = None
    target_id: str | None = None
    # One-line human-readable description.
    summary: str = ""
    # Structured details (old/new values etc.), JSON-encoded.
    metadata_json: str = "{}"
    ip: str | None = None
    user_agent: str | None = None
