import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

# What is being changed. Each is a change the LLD says governance must gate:
# §3.7 — *"Transitions gated by governance policies"* and *"Visibility &
# pricing — changes need governance approval"*.
KIND_LIFECYCLE = "lifecycle"
KIND_VISIBILITY = "visibility"
KIND_PRICING = "pricing"
KINDS = (KIND_LIFECYCLE, KIND_VISIBILITY, KIND_PRICING)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_WITHDRAWN = "withdrawn"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryChangeRequest(SQLModel, table=True):
    """A change to a registry tool that somebody other than the requester must allow.

    The LLD gates two things behind governance: lifecycle transitions into
    review and publication, and changes to visibility or pricing. This row is
    that gate.

    It records **both sides** of the change — what the tool is now and what is
    being asked for — because an approver deciding from the request alone is
    deciding without knowing what it replaces, and because the "before" is what
    makes the audit trail readable a year later.

    The *policy engine* that could decide some of these automatically is
    governance's own work and is not here (ADR-045). What is here is the gate:
    an ungated transition is applied immediately, a gated one is not applied
    until a decision is recorded.
    """

    __tablename__ = "registry_change_request"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    tool_id: uuid.UUID = Field(foreign_key="registry_tool.id", index=True)
    kind: str = Field(index=True)

    # The change, both sides. JSON because the shape differs per kind and a
    # column per field would be a migration every time governance gates one
    # more thing.
    current_json: str = Field(default="{}")
    requested_json: str = Field(default="{}")
    reason: str = ""

    status: str = Field(default=STATUS_PENDING, index=True)
    decision_note: str = ""
    requested_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    decided_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
