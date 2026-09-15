"""Marketplace listings: the read-optimized view over the *integration catalog*.

This is the console's catalog — bundled integrations, remote MCP servers, and
APIs compiled in this org — keyed by integration id. It is not the registry
storefront: that is `/v1/marketplace`, which lists `registry_tool` records
across tenants and is a projection of registry events (LLD §3.7).

The two meet where an integration has been registered. A registry record whose
`integration_id` matches a catalog entry supplies the fields this service used
to return as null: trust score, pricing, versions, declared regions and
compliance. Where there is no registry record — a bundled GitHub integration,
say — those fields stay **explicitly null with a reason**, and the reason is now
the accurate one: nobody has registered it. A trust score of 0 would read as
"this tool is untrustworthy"; null with a reason reads as what it is.
"""

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlmodel import Session, col, select

from sutr.integrations import registry as integration_registry
from sutr.integrations.categories import all_categories, category_for, tags_for
from sutr.models.integration import InstalledIntegration
from sutr.models.marketplace_review import MarketplaceReview

# Why a Registry-owned field is null. Stated once, so every listing says the
# same thing and nobody has to guess whether null means "zero" or "unknown".
PENDING_REGISTRY = (
    "Not available: these fields come from the Registry, and this integration has no registry "
    "record. Register it at POST /v1/registry/tools with this integration_id to populate them."
)

SORTS = ("popular", "rating", "name", "recent")


@dataclass
class Listing:
    integration_id: str
    name: str
    description: str | None
    type: str
    category: str
    tags: list[str]
    docs_url: str | None
    auth_methods: list[str]
    tool_count: int | None
    install_count: int
    installed: bool
    available: bool
    available_reason: str | None
    rating: float | None
    review_count: int
    # Registry-owned. Populated when a registry record names this integration,
    # and left null with a reason when none does.
    trust_score: int | None = None
    compliance: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    pricing: dict | None = None
    versions: list[str] = field(default_factory=list)
    pending_fields: list[str] = field(default_factory=list)
    registry_tool_id: str | None = None
    lifecycle_state: str | None = None

    def as_dict(self) -> dict:
        return {
            "integration_id": self.integration_id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "category": self.category,
            "tags": self.tags,
            "docs_url": self.docs_url,
            "auth_methods": self.auth_methods,
            "tool_count": self.tool_count,
            "install_count": self.install_count,
            "installed": self.installed,
            "available": self.available,
            "available_reason": self.available_reason,
            "rating": self.rating,
            "review_count": self.review_count,
            "trust_score": self.trust_score,
            "compliance": self.compliance,
            "regions": self.regions,
            "pricing": self.pricing,
            "versions": self.versions,
            "registry_tool_id": self.registry_tool_id,
            "lifecycle_state": self.lifecycle_state,
            # `regions` and `compliance` are the provider's own claims; nothing
            # in this platform has verified them.
            "declared_by_provider": ["regions", "compliance"],
            "pending_fields": self.pending_fields,
            "pending_reason": PENDING_REGISTRY if self.pending_fields else None,
        }


_REGISTRY_OWNED = ["trust_score", "compliance", "regions", "pricing", "versions"]


def _tool_count(integration) -> int | None:
    """How many tools this integration exposes, when that is known locally.

    A remote MCP server's tool list lives upstream and is only known after a
    discovery call, so it is `None` here rather than 0 — an unknown count must
    not render as "no tools".
    """
    tools = getattr(integration, "tools", None)
    return len(tools) if tools is not None else None


def install_counts(session: Session) -> dict[str, int]:
    """How many organizations have each integration installed."""
    rows = session.exec(
        select(InstalledIntegration.integration_id, func.count()).group_by(
            InstalledIntegration.integration_id
        )
    ).all()
    return {integration_id: int(count) for integration_id, count in rows}


def rating_summary(session: Session) -> dict[str, tuple[float, int]]:
    """Mean rating and review count per integration."""
    rows = session.exec(
        select(
            MarketplaceReview.integration_id,
            func.avg(MarketplaceReview.rating),
            func.count(),
        ).group_by(MarketplaceReview.integration_id)
    ).all()
    return {
        integration_id: (round(float(average), 2), int(count))
        for integration_id, average, count in rows
    }


def registry_overlay(session: Session, org_id: uuid.UUID) -> dict[str, dict]:
    """Registry facts for this org's integrations, keyed by integration id.

    Scoped to the caller's own registry. Integration ids are global strings and
    another tenant's registry record for `github` says nothing about this
    tenant's install of it — reading one would be a cross-tenant claim dressed
    as a lookup.
    """
    from sutr.models.registry_tool import RegistryTool
    from sutr.registry import pricing as registry_pricing
    from sutr.registry import trust as registry_trust
    from sutr.registry import versions as registry_versions

    overlay: dict[str, dict] = {}
    tools = session.exec(
        select(RegistryTool)
        .where(RegistryTool.org_id == org_id)
        .where(col(RegistryTool.integration_id).is_not(None))
    ).all()
    for tool in tools:
        score = registry_trust.compute(session, tool)
        overlay[str(tool.integration_id)] = {
            "registry_tool_id": str(tool.id),
            "lifecycle_state": tool.lifecycle_state,
            "trust_score": score.score,
            "pricing": registry_pricing.serialize(registry_pricing.current(session, tool.id)),
            "versions": [
                str(version.version)
                for version in registry_versions.list_for_tool(session, tool.id)
            ],
            "regions": json.loads(tool.regions_json or "[]"),
            "compliance": json.loads(tool.compliance_json or "[]"),
        }
    return overlay


def build_listing(
    integration,
    *,
    install_count: int,
    installed: bool,
    rating: float | None,
    review_count: int,
    registry: dict | None = None,
) -> Listing:
    available, reason = integration.is_available()
    if registry:
        return Listing(
            integration_id=integration.id,
            name=integration.name,
            description=integration.description,
            type=integration.type,
            category=category_for(integration),
            tags=tags_for(integration),
            docs_url=getattr(integration, "docs_url", None),
            auth_methods=[auth.method for auth in integration.auth],
            tool_count=_tool_count(integration),
            install_count=install_count,
            installed=installed,
            available=available,
            available_reason=reason,
            rating=rating,
            review_count=review_count,
            registry_tool_id=registry["registry_tool_id"],
            lifecycle_state=registry["lifecycle_state"],
            trust_score=registry["trust_score"],
            pricing=registry["pricing"],
            versions=registry["versions"],
            regions=registry["regions"],
            compliance=registry["compliance"],
            # A trust score that could not be computed is still an answer from
            # the registry, so it is not "pending" — it is null with its own
            # explanation, which the registry endpoint carries.
            pending_fields=[],
        )
    return Listing(
        integration_id=integration.id,
        name=integration.name,
        description=integration.description,
        type=integration.type,
        category=category_for(integration),
        tags=tags_for(integration),
        docs_url=getattr(integration, "docs_url", None),
        auth_methods=[auth.method for auth in integration.auth],
        tool_count=_tool_count(integration),
        install_count=install_count,
        installed=installed,
        available=available,
        available_reason=reason,
        rating=rating,
        review_count=review_count,
        pending_fields=list(_REGISTRY_OWNED),
    )


def _matches(listing: Listing, query: str) -> bool:
    haystack = " ".join(
        [listing.integration_id, listing.name, listing.description or "", *listing.tags]
    ).lower()
    return all(term in haystack for term in query.lower().split())


def _sort_key(sort: str):
    if sort == "rating":
        # Unrated sits below rated, rather than above it by virtue of null.
        return lambda listing: (
            -(listing.rating or -1),
            -listing.review_count,
            listing.name.lower(),
        )
    if sort == "name":
        return lambda listing: listing.name.lower()
    if sort == "recent":
        # Nothing here carries a publication date yet — that is Registry data —
        # so "recent" falls back to name rather than inventing an ordering.
        return lambda listing: listing.name.lower()
    return lambda listing: (-listing.install_count, listing.name.lower())


def list_listings(
    session: Session,
    org_id: uuid.UUID,
    *,
    query: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    type_filter: str | None = None,
    installed_only: bool = False,
    available_only: bool = False,
    sort: str = "popular",
) -> list[Listing]:
    integrations = integration_registry.list_all(org_id=org_id)
    counts = install_counts(session)
    ratings = rating_summary(session)
    installed_here = {
        row.integration_id
        for row in session.exec(
            select(InstalledIntegration).where(InstalledIntegration.org_id == org_id)
        ).all()
    }

    overlay = registry_overlay(session, org_id)

    listings = []
    for integration in integrations:
        rating, review_count = ratings.get(integration.id, (None, 0))
        listings.append(
            build_listing(
                integration,
                install_count=counts.get(integration.id, 0),
                installed=integration.id in installed_here,
                rating=rating,
                review_count=review_count,
                registry=overlay.get(integration.id),
            )
        )

    if query:
        listings = [listing for listing in listings if _matches(listing, query)]
    if category:
        listings = [listing for listing in listings if listing.category == category]
    if tags:
        wanted = {tag.lower() for tag in tags}
        listings = [listing for listing in listings if wanted <= set(listing.tags)]
    if type_filter:
        listings = [listing for listing in listings if listing.type == type_filter]
    if installed_only:
        listings = [listing for listing in listings if listing.installed]
    if available_only:
        listings = [listing for listing in listings if listing.available]

    listings.sort(key=_sort_key(sort if sort in SORTS else "popular"))
    return listings


def get_listing(session: Session, org_id: uuid.UUID, integration_id: str) -> Listing | None:
    integration = integration_registry.get(integration_id, org_id=org_id)
    if integration is None:
        return None
    counts = install_counts(session)
    rating, review_count = rating_summary(session).get(integration_id, (None, 0))
    installed = (
        session.exec(
            select(InstalledIntegration)
            .where(InstalledIntegration.org_id == org_id)
            .where(InstalledIntegration.integration_id == integration_id)
        ).first()
        is not None
    )
    return build_listing(
        integration,
        install_count=counts.get(integration_id, 0),
        installed=installed,
        rating=rating,
        review_count=review_count,
        registry=registry_overlay(session, org_id).get(integration_id),
    )


def category_facets(session: Session, org_id: uuid.UUID) -> list[dict]:
    """Every category with how many listings it holds, for the filter rail."""
    listings = list_listings(session, org_id)
    counts: dict[str, int] = {}
    for listing in listings:
        counts[listing.category] = counts.get(listing.category, 0) + 1
    known = all_categories()
    extra = sorted(set(counts) - set(known))
    return [
        {"category": category, "count": counts.get(category, 0)}
        for category in [*known, *extra]
        if counts.get(category, 0) or category in known
    ]


def tag_facets(session: Session, org_id: uuid.UUID, limit: int = 40) -> list[dict]:
    counts: dict[str, int] = {}
    for listing in list_listings(session, org_id):
        for tag in listing.tags:
            counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"tag": tag, "count": count} for tag, count in ordered[:limit]]
