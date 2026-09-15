import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class ConsumedEvent(SQLModel, table=True):
    """A record that one consumer already handled one event.

    Delivery is at-least-once (ESDS LLD §5.6), so a consumer *will* see the
    same event twice — after a relay restart, a broker redelivery, or a retry
    of a handler that succeeded but failed to acknowledge. "Consumers must be
    idempotent" is only true if something remembers, and this is that thing.

    Keyed by `(consumer, event_id)` rather than `event_id` alone: two consumers
    of the same event are independent, and one having handled it says nothing
    about the other.
    """

    __tablename__ = "consumed_event"
    __table_args__ = (UniqueConstraint("consumer", "event_id", name="uq_consumed_event_consumer"),)

    id: int | None = Field(default=None, primary_key=True)
    consumer: str = Field(index=True)
    event_id: uuid.UUID = Field(index=True)
    event_type: str
    consumed_at: datetime = Field(default_factory=datetime.utcnow)
