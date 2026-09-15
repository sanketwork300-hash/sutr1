import uuid
from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

PENDING = "pending"
PUBLISHED = "published"
FAILED = "failed"
DEAD_LETTERED = "dead_lettered"

# Attempts before a message stops being retried and waits for a human.
# ESDS LLD §5.6: retry, then DLQ, then manual investigation.
MAX_ATTEMPTS = 5


class OutboxEvent(SQLModel, table=True):
    """An event awaiting publication, written in the same transaction as the
    state change it describes.

    This is the producer contract (ADR-003). Publishing directly to a broker
    inside a request has two failure modes and both are silent: the state
    commits and the publish fails, so a fact is lost; or the publish succeeds
    and the transaction rolls back, so a fact is announced that never happened.
    Writing the event to this table in the *same* transaction makes both
    impossible — the fact and its announcement commit together or not at all.

    A relay then moves rows to the bus. That is where at-least-once delivery
    comes from, and why consumers must be idempotent.
    """

    __tablename__ = "outbox_event"
    __table_args__ = (
        # The relay's query: oldest pending first.
        Index("ix_outbox_event_state_id", "state", "id"),
        Index("ix_outbox_event_type_id", "event_type", "id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4, unique=True, index=True)
    event_type: str = Field(index=True)
    event_version: int = Field(default=1)
    # The key that orders this entity's facts. Carried on the row so the relay
    # does not have to re-derive it.
    partition_key: str = ""
    correlation_id: str | None = None
    tenant_id: str | None = Field(default=None, index=True)
    resource_id: str | None = None
    producer: str = "sutr"
    # The complete envelope, as it will be published.
    envelope_json: str

    state: str = Field(default=PENDING, index=True)
    attempts: int = Field(default=0)
    last_error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: datetime | None = None
