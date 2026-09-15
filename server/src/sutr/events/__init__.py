"""The platform's event backbone (ESDS LLD §5.6, ADR-003).

    producer  →  outbox.publish(session, ...)   same transaction as the state change
              →  relay                          moves rows onto the bus
              →  bus                            in-process, or Kafka
              →  consumers.dispatch             idempotent, one group per consumer

Import `publish` from here; everything else is machinery.
"""

from sutr.events.bus import EventBus, InProcessBus, get_bus, reset_bus
from sutr.events.consumers import dispatch, subscribe, unsubscribe
from sutr.events.envelope import EventEnvelope
from sutr.events.outbox import publish
from sutr.events.topics import ALL_TOPICS, partition_key, topic_for

__all__ = [
    "ALL_TOPICS",
    "EventBus",
    "EventEnvelope",
    "InProcessBus",
    "dispatch",
    "get_bus",
    "partition_key",
    "publish",
    "reset_bus",
    "subscribe",
    "topic_for",
    "unsubscribe",
]
