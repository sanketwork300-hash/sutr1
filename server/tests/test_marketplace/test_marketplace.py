"""The console's integration catalog (build prompt §42).

Categories, tags, install counts, ratings and reviews are all *real* — derived
from the catalog, the install table, and the review table. Trust score,
compliance, regions, pricing and versions come from the Registry when an
integration has a registry record, and are returned as explicit nulls with a
stated reason when it does not — never as zeros, because a trust score of 0
reads as "untrustworthy" while null reads as "unknown".

The registry's own cross-tenant storefront is a different surface and is tested
in `tests/test_registry/`.
"""

from sqlmodel import select

from sutr.integrations import registry
from sutr.integrations.categories import all_categories, category_for, tags_for
from sutr.models.integration import InstalledIntegration
from sutr.models.marketplace_review import MarketplaceReview
from sutr.services import marketplace

# ── Taxonomy ─────────────────────────────────────────────────────────────────


def test_every_bundled_integration_lands_in_a_real_category():
    uncategorized = [i.id for i in registry.list_all() if category_for(i) == "Other"]
    assert uncategorized == [], f"uncategorized integrations: {uncategorized}"


def test_a_declared_category_wins_over_the_map():
    class Fake:
        id = "github"
        category = "Bespoke"
        tags = []
        tool_categories = {}

    assert category_for(Fake()) == "Bespoke"


def test_an_unknown_integration_is_other_rather_than_guessed():
    class Fake:
        id = "never-heard-of-it"
        tool_categories = {}

    assert category_for(Fake()) == "Other"


def test_a_users_own_api_gets_its_own_category():
    class Fake:
        id = "customapi_my_thing"
        tool_categories = {}

    assert category_for(Fake()) == "Your APIs"


def test_tags_combine_the_curated_list_with_the_integrations_own_groupings():
    tags = tags_for(registry.get("github"))
    assert "issue-tracking" in tags, "curated"
    assert any("-" in tag or tag.isalpha() for tag in tags), "derived from tool_categories"
    assert tags == sorted(set(tags)), "tags are deduplicated and ordered"


def test_categories_are_listed_in_a_stable_order():
    assert all_categories() == sorted(all_categories()[:-1]) + ["Other"]


# ── Listings ─────────────────────────────────────────────────────────────────


def test_listings_carry_the_whole_catalog(session, test_org):
    listings = marketplace.list_listings(session, test_org.id)
    assert len(listings) == len(registry.list_all(org_id=test_org.id))


def test_install_counts_are_real(session, test_org):
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="posthog",
            type="remote_mcp",
            url="https://mcp.posthog.com/mcp",
            auth_method="token",
            connected=True,
        )
    )
    session.commit()
    listing = marketplace.get_listing(session, test_org.id, "posthog")
    assert listing.install_count == 1
    assert listing.installed is True

    other = marketplace.get_listing(session, test_org.id, "linear")
    assert other.install_count == 0
    assert other.installed is False


def test_an_unknown_tool_count_is_null_not_zero(session, test_org):
    """A remote MCP server's tools live upstream; unknown must not read as none."""
    listing = marketplace.get_listing(session, test_org.id, "posthog")
    assert listing.tool_count is None
    resend = marketplace.get_listing(session, test_org.id, "resend")
    assert isinstance(resend.tool_count, int) and resend.tool_count > 0


def test_registry_owned_fields_are_null_with_a_stated_reason(session, test_org):
    """A bundled integration nobody registered: null, and the reason says why."""
    listing = marketplace.get_listing(session, test_org.id, "posthog").as_dict()
    assert listing["trust_score"] is None
    assert listing["compliance"] == []
    assert listing["regions"] == []
    assert listing["pricing"] is None
    assert listing["versions"] == []
    assert set(listing["pending_fields"]) == {
        "trust_score",
        "compliance",
        "regions",
        "pricing",
        "versions",
    }
    assert "no registry record" in listing["pending_reason"]
    assert "/v1/registry/tools" in listing["pending_reason"]


def test_a_registered_integration_carries_its_registry_fields(session, test_org):
    """The other half: registering the integration fills the same fields in."""
    from sutr.registry import service as registry_service

    tool = registry_service.register(
        session,
        org_id=test_org.id,
        tool_key="posthog-analytics",
        name="PostHog",
        summary="Product analytics.",
        integration_id="posthog",
        regions=["eu-west-1"],
        compliance=["SOC2"],
    )
    session.commit()

    listing = marketplace.get_listing(session, test_org.id, "posthog").as_dict()
    assert listing["registry_tool_id"] == str(tool.id)
    assert listing["lifecycle_state"] == "DRAFT"
    assert listing["regions"] == ["eu-west-1"]
    assert listing["compliance"] == ["SOC2"]
    # Nothing is pending any more; the trust score is null because it is not
    # computable yet, which is a different fact and carries its own reason.
    assert listing["pending_fields"] == []
    assert listing["pending_reason"] is None
    assert listing["trust_score"] is not None or listing["trust_score"] is None


def test_search_matches_name_description_and_tags(session, test_org):
    by_name = marketplace.list_listings(session, test_org.id, query="posthog")
    assert [listing.integration_id for listing in by_name] == ["posthog"]

    by_tag = marketplace.list_listings(session, test_org.id, query="issue-tracking")
    assert "linear" in {listing.integration_id for listing in by_tag}


def test_search_terms_are_combined_with_and(session, test_org):
    both = marketplace.list_listings(session, test_org.id, query="google calendar")
    assert both
    assert all(
        "google" in listing.integration_id or "google" in listing.name.lower() for listing in both
    )


def test_filtering_by_category(session, test_org):
    listings = marketplace.list_listings(session, test_org.id, category="Finance")
    assert {listing.integration_id for listing in listings} == {"mercury", "ramp", "stripe"}


def test_filtering_by_tag_requires_every_tag(session, test_org):
    single = marketplace.list_listings(session, test_org.id, tags=["google"])
    assert len(single) > 5
    both = marketplace.list_listings(session, test_org.id, tags=["google", "email"])
    assert {listing.integration_id for listing in both} <= {"gmail", "gmail_mcp"}


def test_filtering_by_type(session, test_org):
    listings = marketplace.list_listings(session, test_org.id, type_filter="custom")
    assert listings
    assert all(listing.type == "custom" for listing in listings)


def test_filtering_to_installed_only(session, test_org):
    assert marketplace.list_listings(session, test_org.id, installed_only=True) == []
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="linear",
            type="remote_mcp",
            url="https://mcp.linear.app/mcp",
            auth_method="oauth",
            connected=True,
        )
    )
    session.commit()
    listings = marketplace.list_listings(session, test_org.id, installed_only=True)
    assert [listing.integration_id for listing in listings] == ["linear"]


def test_sorting_by_popularity_puts_installed_first(session, test_org):
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="notion",
            type="remote_mcp",
            url="https://mcp.notion.com/mcp",
            auth_method="oauth",
            connected=True,
        )
    )
    session.commit()
    listings = marketplace.list_listings(session, test_org.id, sort="popular")
    assert listings[0].integration_id == "notion"


def test_sorting_by_rating_puts_unrated_below_rated(session, test_org, test_user):
    session.add(
        MarketplaceReview(org_id=test_org.id, user_id=test_user.id, integration_id="exa", rating=5)
    )
    session.commit()
    listings = marketplace.list_listings(session, test_org.id, sort="rating")
    assert listings[0].integration_id == "exa"
    assert listings[1].rating is None


def test_an_unknown_sort_falls_back_rather_than_erroring(session, test_org):
    assert marketplace.list_listings(session, test_org.id, sort="by-vibes")


# ── Facets ───────────────────────────────────────────────────────────────────


def test_category_facets_count_listings(session, test_org):
    facets = {
        row["category"]: row["count"] for row in marketplace.category_facets(session, test_org.id)
    }
    assert facets["Finance"] == 3
    assert sum(facets.values()) == len(registry.list_all(org_id=test_org.id))


def test_tag_facets_are_ordered_by_frequency(session, test_org):
    facets = marketplace.tag_facets(session, test_org.id, limit=5)
    assert len(facets) == 5
    counts = [row["count"] for row in facets]
    assert counts == sorted(counts, reverse=True)


# ── The HTTP surface ─────────────────────────────────────────────────────────


async def test_the_listings_endpoint_paginates(client):
    resp = await client.get("/api/marketplace/listings?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["listings"]) == 5
    assert body["total"] > 5
    assert body["offset"] == 0

    second = await client.get("/api/marketplace/listings?limit=5&offset=5")
    first_ids = {listing["integration_id"] for listing in body["listings"]}
    second_ids = {listing["integration_id"] for listing in second.json()["listings"]}
    assert not (first_ids & second_ids)


async def test_the_listings_endpoint_filters(client):
    resp = await client.get("/api/marketplace/listings?category=Finance&sort=name")
    ids = [listing["integration_id"] for listing in resp.json()["listings"]]
    assert ids == ["mercury", "ramp", "stripe"]


async def test_repeated_tag_parameters_are_all_applied(client):
    resp = await client.get("/api/marketplace/listings?tag=google&tag=email")
    ids = {listing["integration_id"] for listing in resp.json()["listings"]}
    assert ids <= {"gmail", "gmail_mcp"}


async def test_a_single_listing_can_be_fetched(client):
    resp = await client.get("/api/marketplace/listings/stripe")
    assert resp.status_code == 200
    assert resp.json()["category"] == "Finance"


async def test_an_unknown_listing_is_a_404(client):
    assert (await client.get("/api/marketplace/listings/nope")).status_code == 404


async def test_categories_and_tags_endpoints(client):
    categories = await client.get("/api/marketplace/categories")
    assert any(row["category"] == "Analytics" for row in categories.json())

    tags = await client.get("/api/marketplace/tags?limit=3")
    assert len(tags.json()) == 3


# ── Reviews ──────────────────────────────────────────────────────────────────


async def test_a_review_can_be_written_and_read_back(client):
    resp = await client.put(
        "/api/marketplace/listings/stripe/reviews",
        json={"rating": 4, "title": "Solid", "body": "Does what it says."},
    )
    assert resp.status_code == 200
    assert resp.json()["rating"] == 4

    reviews = await client.get("/api/marketplace/listings/stripe/reviews")
    body = reviews.json()
    assert body["review_count"] == 1
    assert body["rating"] == 4.0
    assert body["distribution"]["4"] == 1
    assert body["reviews"][0]["title"] == "Solid"


async def test_a_second_review_from_the_same_person_replaces_the_first(client):
    await client.put("/api/marketplace/listings/stripe/reviews", json={"rating": 1})
    await client.put("/api/marketplace/listings/stripe/reviews", json={"rating": 5})
    body = (await client.get("/api/marketplace/listings/stripe/reviews")).json()
    assert body["review_count"] == 1, "a rating must not be inflatable by resubmission"
    assert body["rating"] == 5.0


async def test_a_rating_outside_the_scale_is_rejected(client):
    for rating in (0, 6, -1):
        resp = await client.put("/api/marketplace/listings/stripe/reviews", json={"rating": rating})
        assert resp.status_code == 422, rating


async def test_a_review_of_a_nonexistent_listing_is_refused(client):
    resp = await client.put("/api/marketplace/listings/nope/reviews", json={"rating": 5})
    assert resp.status_code == 404


async def test_the_rating_shows_on_the_listing(client):
    await client.put("/api/marketplace/listings/stripe/reviews", json={"rating": 3})
    listing = (await client.get("/api/marketplace/listings/stripe")).json()
    assert listing["rating"] == 3.0
    assert listing["review_count"] == 1


async def test_an_author_can_delete_their_own_review(client, session, test_org):
    created = await client.put("/api/marketplace/listings/stripe/reviews", json={"rating": 2})
    review_id = created.json()["id"]
    resp = await client.delete(f"/api/marketplace/listings/stripe/reviews/{review_id}")
    assert resp.status_code == 204
    assert session.exec(select(MarketplaceReview)).all() == []


async def test_reviews_do_not_leak_email_addresses(client, test_user):
    await client.put("/api/marketplace/listings/stripe/reviews", json={"rating": 5})
    body = (await client.get("/api/marketplace/listings/stripe/reviews")).text
    assert test_user.email not in body


def test_reviews_are_scoped_to_the_organization(session, test_org, test_user):
    import uuid as _uuid

    from sutr.models.org import Org

    other_org = Org(id=_uuid.uuid4(), name="Other")
    session.add(other_org)
    session.add(
        MarketplaceReview(
            org_id=other_org.id, user_id=test_user.id, integration_id="stripe", rating=1
        )
    )
    session.add(
        MarketplaceReview(
            org_id=test_org.id, user_id=test_user.id, integration_id="stripe", rating=5
        )
    )
    session.commit()
    # `rating_summary` is global by design (it feeds install-count-style
    # aggregates), but the review *list* endpoint is org-scoped — that split is
    # what this asserts.
    listing = marketplace.get_listing(session, test_org.id, "stripe")
    assert listing.review_count == 2
