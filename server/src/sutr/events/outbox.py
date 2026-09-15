"""Writing events: the transactional outbox.

`publish` adds a row to the caller's session and does **not** commit — the same
discipline `services/audit.py` and `services/metering.py` already use. The
event therefore commits exactly when the state change it describes commits, and
a rolled-back transaction takes its announcement with it.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlmodel import Session, col, select

from sutr.events.envelope import EventEnvelope
from sutr.events.topics import partition_key
from sutr.models.outbox_event import MAX_ATTEMPTS, PENDING, PUBLISHED, OutboxEvent

logger = logging.getLogger(__name__)


def publish(
    session: Session,
    event_type: str,
    *,
    tenant_id: uuid.UUID | str | None = None,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
    producer: str = "sutr",
    event_version: int = 1,
) -> OutboxEvent:
    """Record an event for publication, in the caller's transaction.

    Deliberately does not commit: committing here would announce a fact whose
    state change may still roll back.
    """
    envelope = EventEnvelope.create(
        event_type,
        tenant_id=tenant_id,
        resource_id=resource_id,
        payload=payload,
        producer=producer,
        event_version=event_version,
    )
    row = OutboxEvent(
        event_id=envelope.event_id,
        event_type=envelope.event_type,
        event_version=envelope.event_version,
        partition_key=partition_key(
            envelope.event_type,
            resource_id=envelope.resource_id,
            tenant_id=envelope.tenant_id,
        ),
        correlation_id=envelope.correlation_id,
        tenant_id=envelope.tenant_id,
        resource_id=envelope.resource_id,
        producer=envelope.producer,
        envelope_json=json.dumps(envelope.as_dict()),
    )
    session.add(row)
    return row


def envelope_of(row: OutboxEvent) -> EventEnvelope:
    return EventEnvelope.model_validate(json.loads(row.envelope_json))


def claim_pending(session: Session, limit: int = 100) -> list[OutboxEvent]:
    """The next batch to publish, oldest first.

    Ordered by primary key, which is insertion order, so one entity's facts
    reach the bus in the order they happened even on the in-process backend
    where there are no partitions to guarantee it.
    """
    return list(
        session.exec(
            select(OutboxEvent)
            .where(OutboxEvent.state == PENDING)
            .order_by(col(OutboxEvent.id))
            .limit(limit)
        ).all()
    )


def mark_published(session: Session, row: OutboxEvent) -> None:
    row.state = PUBLISHED
    row.published_at = datetime.utcnow()
    row.last_error = None
    session.add(row)


def mark_failed(session: Session, row: OutboxEvent, error: str) -> None:
    """Record a failed publish, dead-lettering once attempts are exhausted.

    A dead-lettered row is kept, not deleted: it is the record of a fact the
    platform failed to announce, and losing it would hide the failure.
    """
    row.attempts += 1
    row.last_error = error[:1000]
    if row.attempts >= MAX_ATTEMPTS:
        from sutr.models.outbox_event import DEAD_LETTERED

        row.state = DEAD_LETTERED
        logger.error(
            "event %s (%s) dead-lettered after %d attempts: %s",
            row.event_id,
            row.event_type,
            row.attempts,
            error[:200],
            extra={"tenant_id": row.tenant_id, "correlation_id": row.correlation_id},
        )
    session.add(row)


def retry(session: Session, row: OutboxEvent) -> None:
    """Return a dead-lettered or failed event to the queue."""
    row.state = PENDING
    row.attempts = 0
    row.last_error = None
    session.add(row)
