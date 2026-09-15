"""The event backbone (ESDS LLD §5.6, ADR-003).

The properties that make an event bus trustworthy, each pinned:

- an event commits with the state change it describes, or not at all;
- the relay publishes, retries, and eventually dead-letters rather than
  spinning or losing;
- a consumer that sees the same event twice does the work once;
- one consumer's failure does not silence another's.
"""

import pytest
from sqlmodel import select

from sutr.events import consumers, outbox, topics
from sutr.events.bus import InProcessBus, get_bus, reset_bus
from sutr.events.envelope import EventEnvelope
from sutr.events.relay import drain_once
from sutr.models.consumed_event import ConsumedEvent
from sutr.models.outbox_event import (
    DEAD_LETTERED,
    MAX_ATTEMPTS,
    PENDING,
    PUBLISHED,
    OutboxEvent,
)
from sutr.request_context import set_correlation_id


@pytest.fixture(autouse=True)
def _clean_subscriptions():
    consumers._SUBSCRIPTIONS.clear()
    reset_bus()
    yield
    consumers._SUBSCRIPTIONS.clear()
    reset_bus()


@pytest.fixture(autouse=True)
def _engine(session, monkeypatch):
    """The relay and consumers open their own sessions from `db.engine`."""
    monkeypatch.setattr("sutr.db.engine", session.get_bind(), raising=False)
    monkeypatch.setattr("sutr.events.relay.db.engine", session.get_bind(), raising=False)
    monkeypatch.setattr("sutr.events.consumers.db.engine", session.get_bind(), raising=False)


# ── The envelope ─────────────────────────────────────────────────────────────


def test_the_envelope_carries_every_mandated_field():
    envelope = EventEnvelope.create(
        topics.TOOL_PUBLISHED, tenant_id="org-1", resource_id="tool-1", payload={"a": 1}
    )
    dumped = envelope.as_dict()
    for field in (
        "event_id",
        "event_type",
        "event_version",
        "timestamp",
        "correlation_id",
        "tenant_id",
        "resource_id",
        "producer",
        "payload",
    ):
        assert field in dumped, f"{field} missing from the envelope"


def test_the_correlation_id_is_inherited_not_minted():
    """One logical operation stays traceable across every stage that reacts."""
    set_correlation_id("corr-inherited")
    assert EventEnvelope.create(topics.API_UPLOADED).correlation_id == "corr-inherited"


def test_every_declared_topic_has_a_partition_key():
    for event_type in topics.ALL_TOPICS:
        key = topics.partition_key(event_type, resource_id="r", tenant_id="t")
        assert key, f"{event_type} has no partition key, so its ordering is undefined"


def test_partition_keys_follow_the_lld_rules():
    assert topics.spec_for("provider.created").partition_by == "provider_id"
    assert topics.spec_for("tool.published").partition_by == "tool_id"
    assert topics.spec_for("runtime.deployed").partition_by == "runtime_id"
    assert topics.spec_for("invoice.generated").partition_by == "invoice_id"
    assert topics.spec_for("policy.updated").partition_by == "policy_id"


def test_an_unknown_event_type_still_gets_a_key_rather_than_none():
    """A keyless event would spread across partitions and lose ordering
    entirely, which is worse than coarse ordering."""
    assert topics.partition_key("made.up", resource_id=None, tenant_id="org-9") == "org-9"
    assert topics.partition_key("made.up", resource_id=None, tenant_id=None) == "made.up"


# ── The outbox ───────────────────────────────────────────────────────────────


def test_publish_does_not_commit_so_a_rollback_takes_the_event_with_it(session, test_org):
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.rollback()
    assert session.exec(select(OutboxEvent)).all() == []


def test_an_event_commits_with_the_state_change_it_describes(session, test_org):
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()
    row = session.exec(select(OutboxEvent)).one()
    assert row.state == PENDING
    assert row.event_type == topics.API_UPLOADED
    assert row.tenant_id == str(test_org.id)
    assert row.partition_key == "p-1"


def test_the_stored_envelope_round_trips(session, test_org):
    outbox.publish(
        session,
        topics.MCP_GENERATED,
        tenant_id=test_org.id,
        resource_id="tool-1",
        payload={"tool_count": 7},
    )
    session.commit()
    row = session.exec(select(OutboxEvent)).one()
    envelope = outbox.envelope_of(row)
    assert envelope.payload["tool_count"] == 7
    assert envelope.event_id == row.event_id


def test_pending_events_come_back_in_insertion_order(session, test_org):
    for index in range(3):
        outbox.publish(
            session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id=f"p-{index}"
        )
    session.commit()
    rows = outbox.claim_pending(session)
    assert [row.resource_id for row in rows] == ["p-0", "p-1", "p-2"]


# ── The relay ────────────────────────────────────────────────────────────────


def test_the_relay_publishes_pending_events(session, test_org):
    seen = []
    consumers.subscribe("test", topics.API_UPLOADED, lambda envelope: seen.append(envelope))
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()

    assert drain_once() == 1
    session.expire_all()
    assert session.exec(select(OutboxEvent)).one().state == PUBLISHED
    assert [envelope.resource_id for envelope in seen] == ["p-1"]


def test_a_published_event_is_not_published_again(session, test_org):
    consumers.subscribe("test", topics.API_UPLOADED, lambda envelope: None)
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()
    assert drain_once() == 1
    assert drain_once() == 0


def test_a_failed_publish_is_retried_then_dead_lettered(session, test_org, monkeypatch):
    class BrokenBus(InProcessBus):
        def publish(self, envelope, *, topic, key):
            raise RuntimeError("broker unreachable")

    monkeypatch.setattr("sutr.events.relay.get_bus", lambda: BrokenBus())
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()

    for _ in range(MAX_ATTEMPTS):
        assert drain_once() == 0
    session.expire_all()
    row = session.exec(select(OutboxEvent)).one()
    assert row.state == DEAD_LETTERED
    assert row.attempts == MAX_ATTEMPTS
    assert "broker unreachable" in row.last_error


def test_a_failure_stops_the_batch_so_later_facts_do_not_overtake_earlier_ones(
    session, test_org, monkeypatch
):
    """Publishing past a failure would announce a later fact about an entity
    before an earlier one, which is exactly what partition ordering exists to
    prevent."""
    published = []

    class FailsFirst(InProcessBus):
        def publish(self, envelope, *, topic, key):
            if envelope.resource_id == "p-0":
                raise RuntimeError("nope")
            published.append(envelope.resource_id)

    monkeypatch.setattr("sutr.events.relay.get_bus", lambda: FailsFirst())
    for index in range(3):
        outbox.publish(
            session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id=f"p-{index}"
        )
    session.commit()

    drain_once()
    assert published == [], "nothing after the failure should have been published"


def test_a_dead_lettered_event_can_be_returned_to_the_queue(session, test_org):
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()
    row = session.exec(select(OutboxEvent)).one()
    row.state = DEAD_LETTERED
    row.attempts = MAX_ATTEMPTS
    session.add(row)
    session.commit()

    outbox.retry(session, row)
    session.commit()
    session.expire_all()
    row = session.exec(select(OutboxEvent)).one()
    assert row.state == PENDING
    assert row.attempts == 0


# ── Consumers are idempotent ─────────────────────────────────────────────────


def test_a_consumer_handles_an_event_once_however_often_it_is_delivered(session, test_org):
    """Delivery is at-least-once, so this is the property that makes it safe."""
    calls = []
    consumers.subscribe("counter", topics.TOOL_PUBLISHED, lambda envelope: calls.append(1))
    envelope = EventEnvelope.create(topics.TOOL_PUBLISHED, tenant_id=test_org.id)

    consumers.dispatch(envelope)
    consumers.dispatch(envelope)
    consumers.dispatch(envelope)

    assert calls == [1], "at-least-once delivery must not become at-least-once work"


def test_two_consumers_of_one_event_are_independent(session, test_org):
    first, second = [], []
    consumers.subscribe("a", topics.TOOL_PUBLISHED, lambda e: first.append(e))
    consumers.subscribe("b", topics.TOOL_PUBLISHED, lambda e: second.append(e))
    consumers.dispatch(EventEnvelope.create(topics.TOOL_PUBLISHED, tenant_id=test_org.id))
    assert len(first) == 1
    assert len(second) == 1


def test_one_consumer_failing_does_not_silence_another(session, test_org):
    delivered = []

    def explode(envelope):
        raise RuntimeError("handler bug")

    consumers.subscribe("broken", topics.TOOL_PUBLISHED, explode)
    consumers.subscribe("healthy", topics.TOOL_PUBLISHED, lambda e: delivered.append(e))
    consumers.dispatch(EventEnvelope.create(topics.TOOL_PUBLISHED, tenant_id=test_org.id))
    assert len(delivered) == 1


def test_a_failed_handler_can_be_retried(session, test_org):
    """The delivery record is rolled back, so redelivery actually re-runs it."""
    attempts = []

    def flaky(envelope):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")

    consumers.subscribe("flaky", topics.TOOL_PUBLISHED, flaky)
    envelope = EventEnvelope.create(topics.TOOL_PUBLISHED, tenant_id=test_org.id)
    consumers.dispatch(envelope)
    consumers.dispatch(envelope)
    assert len(attempts) == 2


def test_delivery_records_are_kept_per_consumer(session, test_org):
    consumers.subscribe("a", topics.TOOL_PUBLISHED, lambda e: None)
    consumers.subscribe("b", topics.TOOL_PUBLISHED, lambda e: None)
    consumers.dispatch(EventEnvelope.create(topics.TOOL_PUBLISHED, tenant_id=test_org.id))
    rows = session.exec(select(ConsumedEvent)).all()
    assert {row.consumer for row in rows} == {"a", "b"}


def test_an_event_with_no_subscriber_is_simply_not_delivered(session, test_org):
    consumers.dispatch(EventEnvelope.create(topics.PROVIDER_CREATED, tenant_id=test_org.id))
    assert session.exec(select(ConsumedEvent)).all() == []


# ── The bus abstraction ──────────────────────────────────────────────────────


def test_the_default_bus_needs_no_infrastructure():
    bus = get_bus()
    assert bus.name == "in_process"
    assert bus.available() == (True, None)


def test_an_unknown_backend_falls_back_rather_than_failing_to_start(monkeypatch):
    from sutr.config import settings

    monkeypatch.setattr(settings, "event_bus_backend", "rabbitmq")
    reset_bus()
    assert get_bus().name == "in_process"


def test_the_kafka_backend_reports_why_it_is_unavailable():
    from sutr.events.kafka_bus import KafkaBus

    available, reason = KafkaBus(bootstrap_servers="").available()
    assert available is False
    assert "KAFKA_BOOTSTRAP_SERVERS" in reason


def test_selecting_kafka_does_not_crash_when_the_client_is_absent(monkeypatch):
    from sutr.config import settings

    monkeypatch.setattr(settings, "event_bus_backend", "kafka")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", "localhost:9092")
    reset_bus()
    bus = get_bus()
    assert bus.name == "kafka"
    available, reason = bus.available()
    # Either the client is installed and the broker is unreachable, or the
    # client is missing — both are reported, neither raises at import time.
    assert available is False
    assert reason


def test_the_kafka_topic_prefix_is_applied():
    from sutr.events.kafka_bus import KafkaBus

    bus = KafkaBus(bootstrap_servers="localhost:9092", topic_prefix="staging.")
    assert bus.topic("tool.published") == "staging.tool.published"


def test_a_topic_is_named_after_its_event_type():
    assert topics.topic_for(topics.TOOL_PUBLISHED) == "tool.published"
