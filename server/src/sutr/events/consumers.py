"""Consuming events, idempotently.

ESDS LLD §5.6: delivery is at-least-once, so *"consumers must be idempotent"*.
That is a requirement on every handler, and requirements that every handler
must remember are requirements that get forgotten. So the guarantee lives here
instead: `dispatch` records `(consumer, event_id)` before calling a handler and
skips the handler entirely if that pair is already recorded.

A handler therefore does not need to be idempotent by its own construction —
it will not be called twice for the same event by the same consumer. What it
must be is *correct when called once*, which is a far easier property to hold.

Each consumer is its own group (LLD §5.6: "each service owns its consumer
group"), so one consumer failing does not stop another from seeing the event.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from sutr import db
from sutr.events.envelope import EventEnvelope
from sutr.models.consumed_event import ConsumedEvent
from sutr.observability import log_context

logger = logging.getLogger(__name__)

Handler = Callable[[EventEnvelope], None]


@dataclass(frozen=True)
class Subscription:
    consumer: str
    event_type: str
    handler: Handler


_SUBSCRIPTIONS: list[Subscription] = []


def subscribe(consumer: str, event_type: str, handler: Handler) -> Subscription:
    """Register a handler. Returns the subscription so tests can remove it."""
    subscription = Subscription(consumer=consumer, event_type=event_type, handler=handler)
    _SUBSCRIPTIONS.append(subscription)
    return subscription


def unsubscribe(subscription: Subscription) -> None:
    if subscription in _SUBSCRIPTIONS:
        _SUBSCRIPTIONS.remove(subscription)


def subscriptions_for(event_type: str) -> list[Subscription]:
    return [s for s in _SUBSCRIPTIONS if s.event_type == event_type]


def already_consumed(session: Session, consumer: str, event_id) -> bool:
    return (
        session.exec(
            select(ConsumedEvent)
            .where(ConsumedEvent.consumer == consumer)
            .where(ConsumedEvent.event_id == event_id)
        ).first()
        is not None
    )


def _claim(session: Session, subscription: Subscription, envelope: EventEnvelope) -> bool:
    """Record the delivery. False when this consumer has already seen it."""
    session.add(
        ConsumedEvent(
            consumer=subscription.consumer,
            event_id=envelope.event_id,
            event_type=envelope.event_type,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def dispatch(envelope: EventEnvelope) -> None:
    """Deliver one event to every subscriber that has not seen it.

    A handler that raises is logged and does **not** stop the others: consumers
    are independent, and one service's bug must not silence another's. The
    delivery record is rolled back so the event can be redelivered.
    """
    with log_context.bound(
        correlation_id=envelope.correlation_id,
        tenant_id=envelope.tenant_id,
    ):
        for subscription in subscriptions_for(envelope.event_type):
            with Session(db.engine) as session:
                if already_consumed(session, subscription.consumer, envelope.event_id):
                    continue
                if not _claim(session, subscription, envelope):
                    continue
                try:
                    subscription.handler(envelope)
                except Exception:
                    logger.exception(
                        "consumer %s failed on %s (%s)",
                        subscription.consumer,
                        envelope.event_type,
                        envelope.event_id,
                    )
                    _release(session, subscription, envelope)


def _release(session: Session, subscription: Subscription, envelope: EventEnvelope) -> None:
    """Undo the delivery record so a failed handler can be retried."""
    record = session.exec(
        select(ConsumedEvent)
        .where(ConsumedEvent.consumer == subscription.consumer)
        .where(ConsumedEvent.event_id == envelope.event_id)
    ).first()
    if record is not None:
        session.delete(record)
        session.commit()
