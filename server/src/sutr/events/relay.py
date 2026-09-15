"""The relay: moves outbox rows onto the bus.

One loop, one job. It is the only thing that publishes, which is what makes
"every event goes through the outbox" enforceable rather than aspirational.

Failure handling follows the LLD's retry → DLQ → manual investigation
(§5.6): a failed publish increments the row's attempt count and leaves it
pending; after `MAX_ATTEMPTS` the row is dead-lettered and waits for a human,
who can retry it through `POST /v1/events/{event_id}/retry`.
"""

import asyncio
import logging

from sqlmodel import Session

from sutr import db
from sutr.events import outbox
from sutr.events.bus import get_bus
from sutr.events.topics import topic_for
from sutr.observability import log_context

logger = logging.getLogger(__name__)

# How often to look for pending events when the last sweep found none.
IDLE_INTERVAL_SECONDS = 2.0
BATCH_SIZE = 100


def drain_once(limit: int = BATCH_SIZE) -> int:
    """Publish one batch. Returns how many rows were published.

    Synchronous and safe to call directly, which is what makes the relay
    testable without running its loop.
    """
    bus = get_bus()
    published = 0
    with Session(db.engine) as session:
        rows = outbox.claim_pending(session, limit=limit)
        for row in rows:
            envelope = outbox.envelope_of(row)
            with log_context.bound(correlation_id=row.correlation_id, tenant_id=row.tenant_id):
                try:
                    bus.publish(
                        envelope,
                        topic=topic_for(row.event_type),
                        key=row.partition_key or row.event_type,
                    )
                except Exception as exc:
                    logger.warning(
                        "publishing %s (%s) failed: %s", row.event_type, row.event_id, exc
                    )
                    outbox.mark_failed(session, row, str(exc))
                    session.commit()
                    # Stop the batch: continuing past a failure would publish a
                    # later fact about the same entity before an earlier one.
                    break
                outbox.mark_published(session, row)
                session.commit()
                published += 1
    return published


async def relay_loop() -> None:
    """Background loop, started with the application."""
    logger.info("event relay started (bus=%s)", get_bus().name)
    while True:
        try:
            published = drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event relay sweep failed")
            published = 0
        # A full batch means there is probably more waiting, so go straight
        # round again rather than sleeping through a backlog.
        if published < BATCH_SIZE:
            await asyncio.sleep(IDLE_INTERVAL_SECONDS)
