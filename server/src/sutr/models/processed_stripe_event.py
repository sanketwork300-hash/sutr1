from datetime import datetime

from sqlmodel import Field, SQLModel


class ProcessedStripeEvent(SQLModel, table=True):
    """Webhook events already applied, keyed by Stripe's own event id.

    Stripe retries delivery until it gets a 2xx, so the same event can arrive
    several times. Without this table a repeated `checkout.session.completed`
    would re-apply its side effects; the id is the primary key, so a duplicate
    insert fails and the handler can skip instead.
    """

    __tablename__ = "processed_stripe_event"

    event_id: str = Field(primary_key=True)
    event_type: str = ""
    processed_at: datetime = Field(default_factory=datetime.utcnow)
