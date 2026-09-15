"""The marketplace as a projection of registry events (LLD §3.7).

These tests drive the real path — publish, drain the outbox, assert the listing
— rather than calling the projection directly, because "the listing appears when
the event is delivered" is the property under test.
"""

import json

from sqlmodel import select

from sutr.events import relay
from sutr.marketplace import listings, profiles, projection, subscriptions
from sutr.models.marketplace_listing import STATE_DELISTED, STATE_PUBLISHED, MarketplaceListing
from sutr.registry import service, versions

from .conftest import publish


def _drain(times: int = 4) -> int:
    """Drain the outbox until it is empty.

    More than once because a handler can publish further events, and a
    projection that only settles after a second pass would otherwise look
    correct in a test that drains once.
    """
    total = 0
    for _ in range(times):
        moved = relay.drain_once()
        total += moved
        if not moved:
            break
    return total


def test_publishing_a_public_tool_creates_a_listing(session, registered_tool, second_user):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()

    listing = session.exec(select(MarketplaceListing)).one()
    assert listing.tool_id == registered_tool.id
    assert listing.state == STATE_PUBLISHED
    assert listing.name == "Refunds API"
    assert listing.lifecycle_state == "PUBLISHED"
    # Projection provenance is on the row, so staleness is visible.
    assert listing.source_event_type == "tool.published"
    assert listing.source_event_id


def test_nothing_is_listed_before_the_event_is_delivered(session, registered_tool, second_user):
    publish(session, registered_tool, decided_by=second_user.id)
    assert session.exec(select(MarketplaceListing)).all() == []


def test_a_private_tool_is_not_listed_even_when_published(session, registered_tool, second_user):
    """Publication is about lifecycle; visibility is about audience."""
    publish(session, registered_tool, decided_by=second_user.id, visibility="private")
    _drain()
    listing = session.exec(select(MarketplaceListing)).one()
    assert listing.state == STATE_DELISTED


def test_an_update_before_publication_creates_no_listing(session, registered_tool):
    service.update(session, registered_tool, {"name": "Renamed"})
    session.commit()
    _drain()
    assert session.exec(select(MarketplaceListing)).all() == []


def test_an_update_after_publication_refreshes_the_listing(session, registered_tool, second_user):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    service.update(session, registered_tool, {"summary": "Refunds, faster."})
    session.commit()
    _drain()
    listing = session.exec(select(MarketplaceListing)).one()
    assert listing.summary == "Refunds, faster."
    assert listing.source_event_type == "tool.updated"


def test_a_new_version_reaches_the_listing(
    session, registered_tool, second_user, validated_artifact
):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    version = versions.create(session, tool=registered_tool, artifact=validated_artifact)
    from sutr.registry import events as registry_events

    registry_events.version_created(
        session,
        org_id=registered_tool.org_id,
        tool_id=registered_tool.id,
        version=version.version,
        build_hash=version.build_hash,
        artifact_id=version.artifact_id,
        tool_count=version.tool_count,
    )
    session.commit()
    _drain()

    listing = session.exec(select(MarketplaceListing)).one()
    assert [v["version"] for v in json.loads(listing.versions_json)] == [1]


def test_a_price_change_reaches_the_listing(session, registered_tool, second_user):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    request = service.request_pricing(
        session, registered_tool, price={"model": "per_call", "amount_micros": 1500}
    )
    service.decide(session, request, approve=True, decided_by_user_id=second_user.id)
    session.commit()
    _drain()

    listing = session.exec(select(MarketplaceListing)).one()
    assert json.loads(listing.pricing_json)["amount_micros"] == 1500
    assert json.loads(listing.pricing_json)["billed_by_this_platform"] is False


def test_deprecation_keeps_the_listing_and_says_so(session, registered_tool, second_user):
    """A tool nobody should start using must not vanish from under its users."""
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    service.deprecate(session, registered_tool, note="Use refunds-api-v2.")
    session.commit()
    _drain()

    listing = session.exec(select(MarketplaceListing)).one()
    assert listing.state == STATE_PUBLISHED
    assert listing.lifecycle_state == "DEPRECATED"
    assert "refunds-api-v2" in listing.deprecation_note


def test_archiving_delists(session, registered_tool, second_user):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    service.deprecate(session, registered_tool, note="gone")
    service.archive(session, registered_tool)
    session.commit()
    _drain()

    listing = session.exec(select(MarketplaceListing)).one()
    assert listing.state == STATE_DELISTED
    assert (
        listings.get(session, viewer_org_id=registered_tool.org_id, tool_id=registered_tool.id)
        is None
    )


def test_projecting_twice_produces_one_row(session, registered_tool, second_user):
    """The projection must be rebuildable, not merely deduplicated."""
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    projection.project(session, registered_tool, event_id="again", event_type="manual")
    session.commit()
    assert len(session.exec(select(MarketplaceListing)).all()) == 1


def test_the_listing_carries_the_trust_score_and_its_explanation(
    session, registered_tool, second_user
):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    listing = session.exec(select(MarketplaceListing)).one()
    trust = json.loads(listing.trust_json)
    assert listing.trust_score == trust["score"]
    assert trust["components"]
    assert "coverage" in trust


def test_the_listing_carries_the_provider_block(session, registered_tool, second_user):
    profiles.upsert(
        session, registered_tool.org_id, {"display_name": "Acme Payments", "summary": "We refund."}
    )
    session.commit()
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    listing = session.exec(select(MarketplaceListing)).one()
    provider = json.loads(listing.provider_json)
    assert provider["display_name"] == "Acme Payments"
    # A profile nobody has verified is not verified, whatever it says about itself.
    assert provider["verified"] is False


def test_a_provider_without_a_profile_falls_back_to_the_org_name(
    session, registered_tool, second_user, test_org
):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    provider = json.loads(session.exec(select(MarketplaceListing)).one().provider_json)
    assert provider["display_name"] == test_org.name
    assert provider["profile"] is False
    assert "not set up a profile" in provider["note"]


def test_subscriber_counts_are_counted_not_incremented(
    session, registered_tool, second_user, other_org
):
    publish(session, registered_tool, decided_by=second_user.id)
    _drain()
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=registered_tool)
    session.commit()
    _drain()
    assert session.exec(select(MarketplaceListing)).one().subscriber_count == 1

    subscriptions.cancel(session, subscription, reason="done")
    session.commit()
    projection.project(session, registered_tool)
    session.commit()
    assert session.exec(select(MarketplaceListing)).one().subscriber_count == 0


def test_the_projection_describes_what_it_is_derived_from():
    described = projection.describe()
    assert "tool.published" in described["derived_from"]
    assert "can lag" in described["note"]
    assert "nothing authorizes off a listing" in described["note"]
