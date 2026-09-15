"""Gauges that have to be read out of the database, sampled at scrape time.

A counter can be incremented where the thing happens. A gauge like "how many
events are waiting" has no such moment — nobody is there when a queue *stays*
long. So these are sampled: the scrape endpoint calls `sample`, which runs two
indexed queries and writes the answers into the registry.

Sampling at scrape time rather than on a timer means the number a scrape
returns was true when it was returned, which is the property an alert on queue
depth depends on. It also means a scrape does a little database work — two
counts and one min() — which is why the failure path here degrades instead of
raising: a monitoring endpoint that goes down with the database takes away the
metrics that would have explained the outage.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from sutr.models.outbox_event import DEAD_LETTERED, PENDING, OutboxEvent
from sutr.observability.metrics import (
    record_sample_failure,
    set_queue_depth,
    set_relay_lag_seconds,
)

logger = logging.getLogger(__name__)


def sample(session: Session) -> dict:
    """Refresh the database-backed gauges. Returns what was sampled.

    Never raises: a sample that fails counts itself and leaves the previous
    values in place, which read as stale rather than as zero. Zero would be a
    lie an alert would act on.
    """
    try:
        pending = session.exec(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.state == PENDING)
        ).one()
        dead = session.exec(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.state == DEAD_LETTERED)
        ).one()
        oldest = session.exec(
            select(func.min(OutboxEvent.created_at)).where(OutboxEvent.state == PENDING)
        ).one()
    except Exception:  # pragma: no cover - exercised through the degradation test
        record_sample_failure()
        logger.warning("Telemetry gauges could not be sampled", exc_info=True)
        return {"sampled": False}

    lag = _age_seconds(oldest)
    set_queue_depth("outbox_pending", pending)
    set_queue_depth("outbox_dead_letter", dead)
    set_relay_lag_seconds(lag)
    return {
        "sampled": True,
        "outbox_pending": pending,
        "outbox_dead_letter": dead,
        "relay_lag_seconds": lag,
    }


def _age_seconds(timestamp: datetime | None) -> float:
    """Seconds since `timestamp`, or 0.0 when there is nothing waiting.

    Stored timestamps are naive UTC (`datetime.utcnow`), so the comparison is
    made naive rather than assuming a tzinfo the column does not carry. A clock
    that has moved backwards would give a negative age; that is reported as 0
    because "the queue is behind by minus four seconds" is not a fact.
    """
    if timestamp is None:
        return 0.0
    now = datetime.now(timezone.utc)
    reference = now if timestamp.tzinfo else now.replace(tzinfo=None)
    return max(0.0, (reference - timestamp).total_seconds())
