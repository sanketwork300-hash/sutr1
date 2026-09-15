"""The shared library: errors, envelope, pagination, idempotency (LLD §2.6, §5.5).

These are the primitives every later service builds on, so their edges matter
more than their happy paths.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from sutr.common import idempotency, responses
from sutr.common.errors import (
    ERROR_STATUS,
    ConflictError,
    NotFoundError,
    PlatformError,
    RateLimitedError,
    error_body,
)
from sutr.common.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidRequestError,
    Page,
    PageRequest,
    decode_cursor,
    encode_cursor,
)
from sutr.models.idempotency_key import STATE_COMPLETED, IdempotencyKey
from sutr.request_context import set_correlation_id, set_request_id

# ── Errors ───────────────────────────────────────────────────────────────────


def test_every_error_code_maps_to_a_status():
    for code, status in ERROR_STATUS.items():
        assert 400 <= status <= 599, code


def test_the_status_contract_matches_the_lld():
    assert ERROR_STATUS["bad_request"] == 400
    assert ERROR_STATUS["unauthorized"] == 401
    assert ERROR_STATUS["forbidden"] == 403
    assert ERROR_STATUS["not_found"] == 404
    assert ERROR_STATUS["conflict"] == 409
    assert ERROR_STATUS["validation_failed"] == 422
    assert ERROR_STATUS["rate_limited"] == 429
    assert ERROR_STATUS["internal_error"] == 500
    assert ERROR_STATUS["unavailable"] == 503


def test_an_error_carries_a_machine_readable_code_and_a_human_message():
    error = NotFoundError("No deployment with that id.")
    assert error.status == 404
    body = error.body()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "No deployment with that id."


def test_an_unknown_code_falls_back_rather_than_raising():
    assert PlatformError("odd", code="something_new").status == 400


def test_retry_after_travels_with_the_error():
    body = RateLimitedError("Slow down.", retry_after=30).body()
    assert body["error"]["retry_after"] == 30


def test_details_are_included_only_when_present():
    assert "details" not in error_body(code="not_found", message="gone")["error"]
    assert error_body(code="not_found", message="gone", details={"id": "x"})["error"]["details"]


def test_the_error_body_always_has_an_error_object():
    """A client should be able to test one key rather than branch on statuses
    it may not have anticipated."""
    body = error_body(code="conflict", message="clash")
    assert isinstance(body["error"], dict)
    assert isinstance(body["meta"], dict)


# ── Envelope ─────────────────────────────────────────────────────────────────


def test_the_envelope_always_has_data_and_meta():
    wrapped = responses.envelope(None)
    assert "data" in wrapped
    assert isinstance(wrapped["meta"], dict)


def test_the_envelope_picks_up_correlation_ids_from_context():
    set_request_id("req-1")
    set_correlation_id("corr-1")
    meta = responses.envelope({"a": 1})["meta"]
    assert meta["request_id"] == "req-1"
    assert meta["correlation_id"] == "corr-1"


def test_none_valued_extras_are_dropped_from_meta():
    meta = responses.envelope([], next_cursor=None)["meta"]
    assert "next_cursor" not in meta


# ── Pagination ───────────────────────────────────────────────────────────────


def test_a_cursor_round_trips():
    assert decode_cursor(encode_cursor({"id": 42})) == {"id": 42}


def test_an_empty_cursor_decodes_to_nothing():
    assert decode_cursor(None) == {}
    assert decode_cursor("") == {}


def test_a_forged_cursor_is_a_client_error_not_a_server_error():
    with pytest.raises(InvalidRequestError) as excinfo:
        decode_cursor("not-a-real-cursor!!")
    assert excinfo.value.status == 400
    assert excinfo.value.details["parameter"] == "cursor"


def test_a_cursor_that_decodes_to_the_wrong_shape_is_rejected():
    import base64
    import json

    forged = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode().rstrip("=")
    with pytest.raises(InvalidRequestError):
        decode_cursor(forged)


def test_limits_are_bounded():
    assert PageRequest.parse().limit == DEFAULT_LIMIT
    assert PageRequest.parse(limit=10).limit == 10
    with pytest.raises(InvalidRequestError):
        PageRequest.parse(limit=0)
    with pytest.raises(InvalidRequestError):
        PageRequest.parse(limit=MAX_LIMIT + 1)


def test_a_page_over_fetches_by_one_to_know_whether_more_exists():
    """Cheaper than a second COUNT(*) over a table that may be large."""
    assert PageRequest.parse(limit=25).fetch_limit == 26


def test_a_full_page_carries_a_cursor_and_a_partial_page_does_not():
    request = PageRequest.parse(limit=2)
    full = Page.build([{"id": 1}, {"id": 2}, {"id": 3}], request, lambda row: {"id": row["id"]})
    assert len(full.items) == 2
    assert decode_cursor(full.next_cursor) == {"id": 2}

    partial = Page.build([{"id": 1}], request, lambda row: {"id": row["id"]})
    assert partial.next_cursor is None


def test_an_empty_page_has_no_cursor():
    assert Page.build([], PageRequest.parse(), lambda row: {}).next_cursor is None


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_no_key_means_no_idempotency(session, test_org):
    assert (
        idempotency.claim(session, org_id=test_org.id, endpoint="POST /x", key=None, payload={})
        is None
    )
    assert (
        idempotency.claim(session, org_id=test_org.id, endpoint="POST /x", key="   ", payload={})
        is None
    )


def test_a_first_use_reserves_the_key_before_the_work_runs(session, test_org):
    """The reservation must be visible to a concurrent retry, which means it is
    committed before the handler starts — not after it finishes."""
    claimed = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={"a": 1}
    )
    assert isinstance(claimed, idempotency.Claimed)
    stored = session.get(IdempotencyKey, claimed.record.id)
    assert stored is not None, "the reservation was not committed"
    assert stored.state == "in_progress"


def test_a_repeat_after_completion_replays_the_original_result(session, test_org):
    claimed = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={"a": 1}
    )
    idempotency.complete(session, claimed, status_code=201, body={"id": "the-original"})

    replay = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={"a": 1}
    )
    assert isinstance(replay, idempotency.Replay)
    assert replay.status_code == 201
    assert replay.body == {"id": "the-original"}


def test_a_repeat_while_still_running_is_a_conflict(session, test_org):
    idempotency.claim(session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={})
    with pytest.raises(ConflictError) as excinfo:
        idempotency.claim(session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={})
    assert excinfo.value.status == 409
    assert excinfo.value.retry_after == 1


def test_the_same_key_with_a_different_body_is_refused(session, test_org):
    """Replaying the first response would silently discard this request."""
    claimed = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={"a": 1}
    )
    idempotency.complete(session, claimed, status_code=201, body={"id": 1})
    with pytest.raises(ConflictError, match="different request body"):
        idempotency.claim(
            session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={"a": 2}
        )


def test_keys_are_scoped_per_endpoint(session, test_org):
    a = idempotency.claim(session, org_id=test_org.id, endpoint="POST /a", key="same", payload={})
    b = idempotency.claim(session, org_id=test_org.id, endpoint="POST /b", key="same", payload={})
    assert isinstance(a, idempotency.Claimed)
    assert isinstance(b, idempotency.Claimed)


def test_keys_are_scoped_per_tenant(session, test_org):
    from sutr.models.org import Org

    other = Org(id=uuid.uuid4(), name="Other")
    session.add(other)
    session.commit()

    mine = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="same", payload={}
    )
    theirs = idempotency.claim(session, org_id=other.id, endpoint="POST /x", key="same", payload={})
    assert isinstance(mine, idempotency.Claimed)
    assert isinstance(theirs, idempotency.Claimed)


def test_a_failed_attempt_releases_the_key_so_a_retry_actually_runs(session, test_org):
    claimed = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={}
    )
    idempotency.release(session, claimed)
    again = idempotency.claim(session, org_id=test_org.id, endpoint="POST /x", key="k1", payload={})
    assert isinstance(again, idempotency.Claimed)


def test_the_request_hash_ignores_key_order(session, test_org):
    assert idempotency.request_hash({"a": 1, "b": 2}) == idempotency.request_hash({"b": 2, "a": 1})


def test_an_over_long_key_is_refused(session, test_org):
    with pytest.raises(ConflictError):
        idempotency.claim(
            session, org_id=test_org.id, endpoint="POST /x", key="k" * 300, payload={}
        )


def test_expired_keys_are_pruned(session, test_org):
    claimed = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="old", payload={}
    )
    record_id = claimed.record.id
    record = session.get(IdempotencyKey, record_id)
    record.expires_at = datetime.utcnow() - timedelta(hours=1)
    record.state = STATE_COMPLETED
    session.add(record)
    session.commit()

    assert idempotency.prune_expired(session) == 1
    session.expunge_all()
    assert session.get(IdempotencyKey, record_id) is None


# ── Housekeeping ─────────────────────────────────────────────────────────────


def test_the_maintenance_sweep_prunes_expired_keys_and_published_events(session, test_org):
    """The outbox is a queue, not the audit log. Published rows are kept briefly
    so an incident can ask "did that event get out?", then removed."""
    from sutr.events import outbox, topics
    from sutr.maintenance import PUBLISHED_EVENT_RETENTION, run_maintenance_sweep
    from sutr.models.consumed_event import ConsumedEvent
    from sutr.models.outbox_event import DEAD_LETTERED, PUBLISHED, OutboxEvent

    claimed = idempotency.claim(
        session, org_id=test_org.id, endpoint="POST /x", key="stale", payload={}
    )
    record = session.get(IdempotencyKey, claimed.record.id)
    record.expires_at = datetime.utcnow() - timedelta(hours=1)
    session.add(record)

    old = datetime.utcnow() - PUBLISHED_EVENT_RETENTION - timedelta(days=1)
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="old")
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="dead")
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="fresh")
    session.commit()

    rows = {row.resource_id: row for row in session.exec(select(OutboxEvent)).all()}
    rows["old"].state, rows["old"].published_at = PUBLISHED, old
    # A dead-lettered event is a fact the platform failed to announce; pruning
    # it would hide the failure, so it must survive however old it is.
    rows["dead"].state, rows["dead"].published_at = DEAD_LETTERED, old
    rows["fresh"].state, rows["fresh"].published_at = PUBLISHED, datetime.utcnow()
    for row in rows.values():
        session.add(row)
    session.add(
        ConsumedEvent(consumer="c", event_id=rows["old"].event_id, event_type="x", consumed_at=old)
    )
    session.commit()

    counts = run_maintenance_sweep()
    assert counts["idempotency_keys_pruned"] == 1
    assert counts["published_events_pruned"] == 1
    assert counts["consumed_records_pruned"] == 1

    session.expunge_all()
    remaining = {row.resource_id for row in session.exec(select(OutboxEvent)).all()}
    assert remaining == {"dead", "fresh"}
