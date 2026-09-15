"""What is discoverable, and the text it is found by.

Discovery searches **registry tools**: the things that have a lifecycle, a
visibility, a subscription and a trust score, which is exactly what LLD §3.8's
policy filter needs to check. Two sources feed it:

- the caller's **own** registry records, whatever state they are in, because a
  provider looking for their own draft should find it;
- **published public listings** from every tenant, which is what a marketplace
  is for.

The bundled-integration catalog is deliberately *not* re-indexed here. It has
its own search at `/api/marketplace/listings`, it has no lifecycle or
subscription to filter on, and a second ranking of the same catalog would be a
second answer to the same question. A registry tool that names an
`integration_id` carries it through, so a discovery result can still tell an
agent which integration to actually call.

Each candidate carries **both** the text it is matched on and the attributes the
policy filter reads, gathered once. Fetching policy attributes per surviving
candidate later would turn one query into N.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from sutr.models.marketplace_listing import STATE_PUBLISHED, MarketplaceListing
from sutr.models.registry_tool import VISIBILITY_PUBLIC, RegistryTool
from sutr.models.tool_subscription import OPEN_STATES, STATE_ACTIVE, ToolSubscription

# Where a candidate came from. Reported on every hit, because "your own draft"
# and "somebody else's published tool" are different kinds of answer.
SOURCE_OWN = "registry"
SOURCE_LISTING = "marketplace"


@dataclass
class Candidate:
    """One discoverable tool, with everything the filter and ranker will read."""

    tool_id: uuid.UUID
    tool_key: str
    name: str
    summary: str
    description: str
    category: str
    tags: list[str]
    regions: list[str]
    compliance: list[str]
    integration_id: str | None
    provider_org_id: uuid.UUID
    lifecycle_state: str
    visibility: str
    trust_score: int | None
    version: int | None
    deprecated: bool
    deprecation_note: str
    subscriber_count: int
    published_at: datetime | None
    source: str
    # Set for a marketplace-sourced candidate: when the projection last ran, and
    # whether the registry has moved since. This is the LLD's "index lag".
    projected_at: datetime | None = None
    stale: bool = False
    # Filled by the policy filter rather than fetched per candidate later.
    subscription_state: str | None = None
    owned: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        """What lexical retrieval matches against.

        The key, name and summary are weighted by repetition rather than by a
        field-boost parameter: BM25 has no notion of fields, and repeating the
        short high-signal text is the standard way to say "this matters more"
        without inventing a scoring knob nobody can tune.
        """
        return " ".join(
            [
                self.tool_key,
                self.tool_key.replace("-", " ").replace("_", " "),
                self.name,
                self.name,
                self.summary,
                self.summary,
                self.description,
                self.category,
                " ".join(self.tags),
            ]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": str(self.tool_id),
            "tool_key": self.tool_key,
            "name": self.name,
            "summary": self.summary,
            "category": self.category,
            "tags": self.tags,
            "regions": self.regions,
            "compliance": self.compliance,
            "declared_by_provider": ["regions", "compliance"],
            "integration_id": self.integration_id,
            "provider_org_id": str(self.provider_org_id),
            "lifecycle_state": self.lifecycle_state,
            "visibility": self.visibility,
            "trust_score": self.trust_score,
            "version": self.version,
            "deprecated": self.deprecated,
            "deprecation_note": self.deprecation_note or None,
            "subscriber_count": self.subscriber_count,
            "source": self.source,
            "stale": self.stale,
            "projected_at": self.projected_at.isoformat() if self.projected_at else None,
            "subscription_state": self.subscription_state,
            "owned": self.owned,
        }


def _as_utc(value: datetime | None) -> datetime | None:
    """Stored datetimes come back naive; compare them as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _from_tool(tool: RegistryTool, *, owned: bool) -> Candidate:
    return Candidate(
        tool_id=tool.id,
        tool_key=tool.tool_key,
        name=tool.name,
        summary=tool.summary,
        description=tool.description,
        category=tool.category,
        tags=json.loads(tool.tags_json or "[]"),
        regions=json.loads(tool.regions_json or "[]"),
        compliance=json.loads(tool.compliance_json or "[]"),
        integration_id=tool.integration_id,
        provider_org_id=tool.org_id,
        lifecycle_state=tool.lifecycle_state,
        visibility=tool.visibility,
        trust_score=None,
        version=tool.published_version,
        deprecated=bool(tool.deprecation_note),
        deprecation_note=tool.deprecation_note,
        subscriber_count=0,
        published_at=_as_utc(tool.published_at),
        source=SOURCE_OWN,
        owned=owned,
    )


def _from_listing(listing: MarketplaceListing, *, stale: bool) -> Candidate:
    return Candidate(
        tool_id=listing.tool_id,
        tool_key=listing.tool_key,
        name=listing.name,
        summary=listing.summary,
        description=listing.description,
        category=listing.category,
        tags=json.loads(listing.tags_json or "[]"),
        regions=json.loads(listing.regions_json or "[]"),
        compliance=json.loads(listing.compliance_json or "[]"),
        integration_id=listing.integration_id,
        provider_org_id=listing.provider_org_id,
        lifecycle_state=listing.lifecycle_state,
        visibility=listing.visibility,
        trust_score=listing.trust_score,
        version=listing.version,
        deprecated=bool(listing.deprecation_note),
        deprecation_note=listing.deprecation_note,
        subscriber_count=listing.subscriber_count,
        published_at=_as_utc(listing.published_at),
        source=SOURCE_LISTING,
        projected_at=_as_utc(listing.projected_at),
        stale=stale,
    )


def collect(session: Session, *, org_id: uuid.UUID, limit: int = 2000) -> list[Candidate]:
    """Everything this tenant could discover, with policy attributes attached.

    A tenant's own registry record wins over its listing when both exist: the
    record is authoritative and the listing may be an event behind, and
    answering a provider's search about their own tool from a stale projection
    would be the projection making a claim it is not entitled to make.
    """
    own = session.exec(select(RegistryTool).where(RegistryTool.org_id == org_id).limit(limit)).all()
    candidates = {tool.id: _from_tool(tool, owned=True) for tool in own}

    listings = session.exec(
        select(MarketplaceListing)
        .where(MarketplaceListing.state == STATE_PUBLISHED)
        .where(MarketplaceListing.visibility == VISIBILITY_PUBLIC)
        .limit(limit)
    ).all()
    lagging = _lagging(session, [listing.tool_id for listing in listings])
    for listing in listings:
        if listing.tool_id in candidates:
            # Own record already present; take the projection's trust score and
            # subscriber count, which the record does not carry.
            candidates[listing.tool_id].trust_score = listing.trust_score
            candidates[listing.tool_id].subscriber_count = listing.subscriber_count
            continue
        candidates[listing.tool_id] = _from_listing(listing, stale=listing.tool_id in lagging)

    _attach_subscriptions(session, org_id, candidates)
    return list(candidates.values())


def _lagging(session: Session, tool_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Listings whose registry record has moved since they were projected.

    This is LLD §3.8's *index lag*: the answer is served from what is indexed
    and flagged stale, rather than blocking on a projection catching up.
    """
    if not tool_ids:
        return set()
    rows = session.exec(
        select(RegistryTool.id, RegistryTool.updated_at).where(col(RegistryTool.id).in_(tool_ids))
    ).all()
    updated = {tool_id: _as_utc(moment) for tool_id, moment in rows}
    listings = session.exec(
        select(MarketplaceListing.tool_id, MarketplaceListing.projected_at).where(
            col(MarketplaceListing.tool_id).in_(tool_ids)
        )
    ).all()
    stale = set()
    for tool_id, projected in listings:
        record_moved = updated.get(tool_id)
        projected_at = _as_utc(projected)
        if record_moved and projected_at and record_moved > projected_at:
            stale.add(tool_id)
    return stale


def _attach_subscriptions(
    session: Session, org_id: uuid.UUID, candidates: dict[uuid.UUID, Candidate]
) -> None:
    if not candidates:
        return
    rows = session.exec(
        select(ToolSubscription)
        .where(ToolSubscription.org_id == org_id)
        .where(col(ToolSubscription.state).in_(OPEN_STATES))
    ).all()
    for subscription in rows:
        candidate = candidates.get(subscription.tool_id)
        if candidate is not None:
            candidate.subscription_state = subscription.state


def entitled(candidate: Candidate) -> bool:
    return candidate.owned or candidate.subscription_state == STATE_ACTIVE
