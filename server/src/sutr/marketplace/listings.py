"""Reading the storefront.

Every query here starts from the projection, never from the registry, and every
one of them applies the same two conditions before anything else: the listing is
published, and the viewer is allowed to see it. Those conditions are in the
`WHERE` clause rather than in a filter over the results, because a storefront
query runs *across tenants* — it is the one place in this codebase that does —
and a cross-tenant query that filters in Python is one refactor away from
filtering nowhere.

Ranking is deliberately simple and deliberately explicit about what it does
with a missing trust score: a tool nobody has measured sorts *below* measured
tools rather than above them by virtue of `NULL`, and it does not sort below
tools that scored badly for nothing — it sorts among them, and the score is
shown so a reader can tell the difference.
"""

import json
import uuid
from typing import Any

from sqlmodel import Session, col, or_, select

from sutr.models.marketplace_listing import STATE_PUBLISHED, MarketplaceListing
from sutr.models.registry_tool import VISIBILITY_PUBLIC

SORTS = ("trust", "subscribers", "recent", "name")
DEFAULT_SORT = "trust"


def _visible(statement, viewer_org_id: uuid.UUID):
    """Published, and either public or the viewer's own."""
    return statement.where(MarketplaceListing.state == STATE_PUBLISHED).where(
        or_(
            MarketplaceListing.visibility == VISIBILITY_PUBLIC,
            MarketplaceListing.provider_org_id == viewer_org_id,
        )
    )


def get(
    session: Session, *, viewer_org_id: uuid.UUID, tool_id: uuid.UUID
) -> MarketplaceListing | None:
    return session.exec(
        _visible(select(MarketplaceListing), viewer_org_id).where(
            MarketplaceListing.tool_id == tool_id
        )
    ).first()


def search(
    session: Session,
    *,
    viewer_org_id: uuid.UUID,
    query: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    min_trust: int | None = None,
    provider_org_id: uuid.UUID | None = None,
    sort: str = DEFAULT_SORT,
    limit: int = 50,
    offset: int = 0,
) -> list[MarketplaceListing]:
    statement = _visible(select(MarketplaceListing), viewer_org_id)
    if category:
        statement = statement.where(MarketplaceListing.category == category)
    if provider_org_id is not None:
        statement = statement.where(MarketplaceListing.provider_org_id == provider_org_id)
    if min_trust is not None:
        # A listing with no score is excluded by a trust floor rather than
        # treated as zero: the caller asked for tools known to be good, and an
        # unmeasured tool is not known to be anything.
        statement = statement.where(col(MarketplaceListing.trust_score) >= min_trust)

    rows = list(session.exec(statement).all())
    if query:
        rows = [row for row in rows if _matches(row, query)]
    if tags:
        wanted = {tag.lower() for tag in tags}
        rows = [row for row in rows if wanted <= {t.lower() for t in json.loads(row.tags_json)}]
    rows.sort(key=_sort_key(sort if sort in SORTS else DEFAULT_SORT))
    return rows[offset : offset + limit]


def _matches(listing: MarketplaceListing, query: str) -> bool:
    haystack = " ".join(
        [
            listing.tool_key,
            listing.name,
            listing.summary,
            listing.description,
            " ".join(json.loads(listing.tags_json or "[]")),
        ]
    ).lower()
    return all(term in haystack for term in query.lower().split())


def _sort_key(sort: str):
    if sort == "subscribers":
        return lambda listing: (-listing.subscriber_count, listing.name.lower())
    if sort == "name":
        return lambda listing: listing.name.lower()
    if sort == "recent":
        return lambda listing: (
            -(listing.published_at.timestamp() if listing.published_at else 0),
            listing.name.lower(),
        )
    # Unscored sits below scored, rather than above it by virtue of null.
    return lambda listing: (
        -(listing.trust_score if listing.trust_score is not None else -1),
        -listing.subscriber_count,
        listing.name.lower(),
    )


def categories(session: Session, *, viewer_org_id: uuid.UUID) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for listing in session.exec(_visible(select(MarketplaceListing), viewer_org_id)).all():
        counts[listing.category] = counts.get(listing.category, 0) + 1
    return [
        {"category": category, "count": count}
        for category, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def serialize(listing: MarketplaceListing, *, detail: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool_id": str(listing.tool_id),
        "tool_key": listing.tool_key,
        "name": listing.name,
        "summary": listing.summary,
        "category": listing.category,
        "tags": json.loads(listing.tags_json or "[]"),
        "regions": json.loads(listing.regions_json or "[]"),
        "compliance": json.loads(listing.compliance_json or "[]"),
        "declared_by_provider": ["regions", "compliance"],
        "integration_id": listing.integration_id,
        "lifecycle_state": listing.lifecycle_state,
        "deprecated": bool(listing.deprecation_note),
        "deprecation_note": listing.deprecation_note or None,
        "version": listing.version,
        "pricing": json.loads(listing.pricing_json or "null"),
        "provider": json.loads(listing.provider_json or "{}"),
        "trust_score": listing.trust_score,
        "subscriber_count": listing.subscriber_count,
        "published_at": listing.published_at.isoformat() if listing.published_at else None,
        # Projection provenance, on every listing rather than in the docs: a
        # reader can see how current this row is instead of assuming.
        "projected_at": listing.projected_at.isoformat(),
        "projected_from": listing.source_event_type or None,
    }
    if detail:
        payload["description"] = listing.description
        payload["versions"] = json.loads(listing.versions_json or "[]")
        payload["trust"] = json.loads(listing.trust_json or "{}")
    return payload
