"""The Kafka backend (ESDS LLD §5.6).

Optional by construction: the client library is not a dependency of Sutr, and
this module is imported only when `EVENT_BUS_BACKEND=kafka`. A missing library
or an unreachable broker is reported as an unavailable backend, not as an
import error at startup — the platform must be able to say *why* it is
degraded (ADR-015).

Not implemented here, deliberately: Avro serialization and Apicurio schema
registration. Both need a running registry to be verified against, and
inventing a serialization the registry may reject would be worse than JSON that
demonstrably round-trips. The envelope carries `event_version`, so moving to
Avro later is a serialization change rather than a contract change. Recorded as
a known limitation rather than a silent gap.
"""

import json
import logging

from sutr.events.envelope import EventEnvelope

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    "The Kafka backend needs a Kafka client. Install one with: "
    "uv sync --extra kafka  (adds confluent-kafka)"
)


class KafkaBus:
    name = "kafka"

    def __init__(self, bootstrap_servers: str, topic_prefix: str = ""):
        self.bootstrap_servers = bootstrap_servers
        self.topic_prefix = topic_prefix
        self._producer = None
        self._unavailable_reason: str | None = None
        if not bootstrap_servers:
            self._unavailable_reason = (
                "KAFKA_BOOTSTRAP_SERVERS is not set, so there is no broker to publish to."
            )

    def _get_producer(self):
        if self._producer is not None:
            return self._producer
        if self._unavailable_reason:
            raise RuntimeError(self._unavailable_reason)
        try:
            from confluent_kafka import Producer  # type: ignore[import-not-found]
        except ImportError:
            self._unavailable_reason = INSTALL_HINT
            raise RuntimeError(INSTALL_HINT)
        self._producer = Producer(
            {
                "bootstrap.servers": self.bootstrap_servers,
                # At-least-once from the producer side: wait for all in-sync
                # replicas, and let the client retry rather than dropping.
                "acks": "all",
                "enable.idempotence": True,
                "retries": 5,
            }
        )
        return self._producer

    def topic(self, event_type: str) -> str:
        return f"{self.topic_prefix}{event_type}" if self.topic_prefix else event_type

    def publish(self, envelope: EventEnvelope, *, topic: str, key: str) -> None:
        producer = self._get_producer()
        producer.produce(
            self.topic(topic),
            key=key.encode(),
            value=json.dumps(envelope.as_dict()).encode(),
            headers=[
                ("event_type", envelope.event_type.encode()),
                ("event_version", str(envelope.event_version).encode()),
                ("correlation_id", (envelope.correlation_id or "").encode()),
            ],
        )
        # Block until the broker acknowledges: the relay marks the outbox row
        # published on return, and marking a row published that the broker
        # never accepted would lose the event.
        remaining = producer.flush(10.0)
        if remaining:
            raise RuntimeError(f"Kafka did not acknowledge {remaining} message(s) within 10s.")

    def available(self) -> tuple[bool, str | None]:
        if self._unavailable_reason:
            return False, self._unavailable_reason
        try:
            self._get_producer()
        except RuntimeError as exc:
            return False, str(exc)
        return True, None

    def close(self) -> None:
        if self._producer is not None:
            try:
                self._producer.flush(5.0)
            except Exception:  # pragma: no cover — best effort on shutdown
                logger.warning("flushing the Kafka producer failed", exc_info=True)
            self._producer = None
