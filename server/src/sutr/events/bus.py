"""The event bus: one interface, two backends.

ADR-003. Kafka is what the ESDS LLD specifies (§5.6) and what a production
deployment should use. It is also a broker, and Sutr's self-hosted install is a
single container with SQLite and no broker at all. Making Kafka mandatory would
break that install; pretending events are optional would break the LLD. So the
bus is an interface, Kafka is one implementation, and the default needs no
infrastructure.

What differs between the backends is stated rather than glossed:

| | in-process | kafka |
|---|---|---|
| Ordering | per entity, by outbox insertion order | per partition, by key |
| Delivery | at-least-once | at-least-once |
| Consumers | in this process only | any consumer group |
| Survives restart | yes — the outbox is a table | yes |

Both are at-least-once, so consumers must be idempotent either way. That is the
property `events/consumers.py` enforces, and it is the same code on both.
"""

import logging
from typing import Protocol

from sutr.events.envelope import EventEnvelope

logger = logging.getLogger(__name__)


class EventBus(Protocol):
    """Somewhere to put an event so consumers see it."""

    name: str

    def publish(self, envelope: EventEnvelope, *, topic: str, key: str) -> None:
        """Publish one event. Raises on failure so the relay can retry."""

    def available(self) -> tuple[bool, str | None]:
        """(usable, reason-if-not) — reported by /v1/platform/capabilities."""

    def close(self) -> None: ...


class InProcessBus:
    """The default: deliver to handlers registered in this process.

    Not a toy. The outbox has already made the event durable, so a handler that
    fails is retried from the table, and a restart loses nothing. What it
    cannot do is deliver to another process — which is exactly what Kafka is
    for, and why the backend is a configuration choice rather than a rewrite.
    """

    name = "in_process"

    def publish(self, envelope: EventEnvelope, *, topic: str, key: str) -> None:
        from sutr.events import consumers

        consumers.dispatch(envelope)

    def available(self) -> tuple[bool, str | None]:
        return True, None

    def close(self) -> None:
        return None


_bus: EventBus | None = None


def get_bus() -> EventBus:
    """The configured bus, built once."""
    global _bus
    if _bus is None:
        _bus = _build_bus()
    return _bus


def reset_bus() -> None:
    """Drop the cached bus. For tests and for reconfiguration."""
    global _bus
    if _bus is not None:
        try:
            _bus.close()
        except Exception:  # pragma: no cover — best effort
            logger.warning("closing the event bus failed", exc_info=True)
    _bus = None


def _build_bus() -> EventBus:
    from sutr.config import settings

    backend = (settings.event_bus_backend or "in_process").strip().lower()
    if backend == "kafka":
        from sutr.events.kafka_bus import KafkaBus

        return KafkaBus(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            topic_prefix=settings.kafka_topic_prefix,
        )
    if backend != "in_process":
        logger.warning(
            "unknown EVENT_BUS_BACKEND %r; falling back to in_process", settings.event_bus_backend
        )
    return InProcessBus()
