"""Building the storefront from Registry events.

LLD §3.7: *"Marketplace = read-optimized storefront derived from Registry
events"*. This module is the derivation, and it is the platform's first real
consumer — the machinery in `events/consumers.py` existed before anything used
it, which is a good way for machinery to be quietly wrong.

Five events shape a listing:

    tool.published    the listing appears, or reappears
    tool.updated      the copy changes; the listing is refreshed if it exists
    version.created   a new version is added to the listing's history
    pricing.updated   the price changes
    tool.deprecated   the listing stays, and says it is deprecated
    tool.archived     the listing is delisted

**A listing is rebuilt from the registry, not from the event payload.** The
event says *what changed*; the handler then reads the record. The alternative —
patching the listing from the payload — makes every event carry a full copy of
the tool and makes the two disagree the first time a payload field is added
without a matching handler change.

**Deprecation does not delist.** A tool nobody should start using is not a tool
that should vanish from under the people already using it; the listing stays and
carries the note. Archiving delists, because an archived tool cannot be
subscribed to at all.

Handlers are idempotent by construction anyway (`consumers.dispatch` will not
call one twice for the same event), but each is also written so that running it
twice produces the same row — a projection that only works because of the
dedupe table is a projection that cannot be rebuilt.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from sutr import db
from sutr.events import EventEnvelope, consumers, topics
from sutr.marketplace import profiles
from sutr.models.marketplace_listing import (
    STATE_DELISTED,
    STATE_PUBLISHED,
    MarketplaceListing,
)
from sutr.models.registry_tool import (
    ARCHIVED,
    VISIBILITY_PRIVATE,
    RegistryTool,
)
from sutr.models.tool_subscription import OPEN_STATES, ToolSubscription
from sutr.registry import pricing, trust, versions

logger = logging.getLogger(__name__)

CONSUMER = "marketplace_projection"

# Event types this projection consumes, in the order a tool normally produces
# them. Declared as data so `describe()` can report what the storefront is
# derived from without anyone reading the handlers.
CONSUMED = (
    topics.TOOL_PUBLISHED,
    topics.TOOL_UPDATED,
    topics.VERSION_CREATED,
    topics.PRICING_UPDATED,
    topics.TOOL_DEPRECATED,
    topics.TOOL_ARCHIVED,
    topics.SUBSCRIPTION_CREATED,
)

_installed: list = []


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def listing_for(session: Session, tool_id: uuid.UUID) -> MarketplaceListing | None:
    return session.exec(
        select(MarketplaceListing).where(MarketplaceListing.tool_id == tool_id)
    ).first()


def _subscriber_count(session: Session, tool_id: uuid.UUID) -> int:
    """Counted, never incremented.

    An increment is a number that drifts the first time an event is replayed or
    a subscription is cancelled behind the projection's back.
    """
    return session.exec(
        select(func.count())
        .select_from(ToolSubscription)
        .where(ToolSubscription.tool_id == tool_id)
        .where(ToolSubscription.state.in_(OPEN_STATES))  # type: ignore[attr-defined]
    ).one()


def project(
    session: Session,
    tool: RegistryTool,
    *,
    event_id: str = "",
    event_type: str = "",
    delist: bool = False,
) -> MarketplaceListing:
    """Rebuild one listing from the registry record. Idempotent."""
    listing = listing_for(session, tool.id)
    if listing is None:
        listing = MarketplaceListing(tool_id=tool.id, provider_org_id=tool.org_id)

    score = trust.compute(session, tool)
    history = versions.list_for_tool(session, tool.id)

    listing.provider_org_id = tool.org_id
    listing.tool_key = tool.tool_key
    listing.name = tool.name
    listing.summary = tool.summary
    listing.description = tool.description
    listing.category = tool.category
    listing.tags_json = tool.tags_json
    listing.regions_json = tool.regions_json
    listing.compliance_json = tool.compliance_json
    listing.integration_id = tool.integration_id
    listing.visibility = tool.visibility
    listing.lifecycle_state = tool.lifecycle_state
    listing.deprecation_note = tool.deprecation_note
    listing.version = tool.published_version
    listing.versions_json = json.dumps([versions.serialize(v) for v in history])
    listing.pricing_json = json.dumps(pricing.serialize(pricing.current(session, tool.id)))
    listing.provider_json = json.dumps(profiles.summary(session, tool.org_id))
    listing.trust_score = score.score
    listing.trust_json = json.dumps(score.as_dict())
    listing.subscriber_count = _subscriber_count(session, tool.id)
    listing.published_at = tool.published_at
    # A private tool has no business on a storefront even if it reached
    # PUBLISHED: publication is about lifecycle, visibility is about audience.
    hidden = delist or tool.lifecycle_state == ARCHIVED or tool.visibility == VISIBILITY_PRIVATE
    listing.state = STATE_DELISTED if hidden else STATE_PUBLISHED
    listing.source_event_id = event_id
    listing.source_event_type = event_type
    listing.projected_at = _utcnow()
    session.add(listing)
    return listing


def _handle(envelope: EventEnvelope, *, delist: bool = False, create: bool = True) -> None:
    tool_id = (envelope.payload or {}).get("tool_id")
    if not tool_id:
        return
    with Session(db.engine) as session:
        tool = session.get(RegistryTool, uuid.UUID(tool_id))
        if tool is None:
            # The record is gone. Nothing to project from, and inventing a
            # listing from the payload would be projecting a memory.
            logger.info("marketplace projection skipped: tool %s no longer exists", tool_id)
            return
        if not create and listing_for(session, tool.id) is None:
            # An update to a tool that was never published is not a listing.
            return
        project(
            session,
            tool,
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
            delist=delist,
        )
        session.commit()


def on_published(envelope: EventEnvelope) -> None:
    _handle(envelope)


def on_updated(envelope: EventEnvelope) -> None:
    _handle(envelope, create=False)


def on_archived(envelope: EventEnvelope) -> None:
    _handle(envelope, delist=True, create=False)


def install() -> None:
    """Register the handlers. Safe to call twice."""
    if _installed:
        return
    for event_type, handler in (
        (topics.TOOL_PUBLISHED, on_published),
        (topics.TOOL_UPDATED, on_updated),
        (topics.VERSION_CREATED, on_updated),
        (topics.PRICING_UPDATED, on_updated),
        (topics.TOOL_DEPRECATED, on_updated),
        (topics.SUBSCRIPTION_CREATED, on_updated),
        (topics.TOOL_ARCHIVED, on_archived),
    ):
        _installed.append(consumers.subscribe(CONSUMER, event_type, handler))


def uninstall() -> None:
    """Remove the handlers. For tests, which must not leak subscriptions."""
    while _installed:
        consumers.unsubscribe(_installed.pop())


def describe() -> dict:
    return {
        "consumer": CONSUMER,
        "derived_from": list(CONSUMED),
        "installed": bool(_installed),
        "note": (
            "Listings are a projection of the registry and can lag behind it. Every listing "
            "records the event it was built from; nothing authorizes off a listing."
        ),
    }
