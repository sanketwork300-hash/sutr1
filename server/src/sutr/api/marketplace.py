"""Marketplace: browse, filter, and rate the tool catalog.

Separate from `/api/integrations`, which is the raw catalog the console and
the CLI already depend on and which must not change shape. The marketplace is
a *storefront view* of the same data — categorised, counted, rated, and
filterable — served on its own routes (build prompt §42).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from sutr.authz import ensure_agent_can
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth, get_current_user
from sutr.marketplace import events as marketplace_events
from sutr.models.marketplace_review import MAX_RATING, MIN_RATING, MarketplaceReview
from sutr.models.user import User
from sutr.services import marketplace

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


class ReviewRequest(BaseModel):
    rating: int = Field(ge=MIN_RATING, le=MAX_RATING)
    title: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=4000)


def _serialize_review(review: MarketplaceReview, author: User | None) -> dict:
    return {
        "id": str(review.id),
        "integration_id": review.integration_id,
        "rating": review.rating,
        "title": review.title,
        "body": review.body,
        # The local part of the author's address, not the address: a review
        # list is read by everyone in the org and does not need to hand out
        # addresses to be scraped.
        "author": author.email.split("@")[0] if author else None,
        "author_user_id": str(review.user_id),
        "created_at": review.created_at.isoformat(),
        "updated_at": review.updated_at.isoformat(),
    }


@router.get("/listings")
def list_listings(
    q: str | None = None,
    category: str | None = None,
    tag: list[str] | None = Query(default=None),
    type: str | None = None,
    installed: bool = False,
    available: bool = False,
    sort: str = "popular",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The storefront. Every filter is optional; none of them is required."""
    listings = marketplace.list_listings(
        session,
        agent_auth.org.id,
        query=q,
        category=category,
        tags=tag,
        type_filter=type,
        installed_only=installed,
        available_only=available,
        sort=sort,
    )
    window = listings[offset : offset + limit]
    return {
        "total": len(listings),
        "limit": limit,
        "offset": offset,
        "sort": sort if sort in marketplace.SORTS else "popular",
        "listings": [listing.as_dict() for listing in window],
    }


@router.get("/categories")
def categories(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    return marketplace.category_facets(session, agent_auth.org.id)


@router.get("/tags")
def tags(
    limit: int = Query(default=40, ge=1, le=200),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    return marketplace.tag_facets(session, agent_auth.org.id, limit=limit)


@router.get("/listings/{integration_id}")
def get_listing(
    integration_id: str,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    listing = marketplace.get_listing(session, agent_auth.org.id, integration_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing.as_dict()


@router.get("/listings/{integration_id}/reviews")
def list_reviews(
    integration_id: str,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Reviews written inside this organization.

    Reviews are org-local: this instance's ratings are its own, and nothing
    here is published anywhere.
    """
    reviews = session.exec(
        select(MarketplaceReview)
        .where(MarketplaceReview.org_id == agent_auth.org.id)
        .where(MarketplaceReview.integration_id == integration_id)
        .order_by(col(MarketplaceReview.updated_at).desc())
    ).all()
    authors = (
        {
            user.id: user
            for user in session.exec(
                select(User).where(col(User.id).in_([review.user_id for review in reviews]))
            ).all()
        }
        if reviews
        else {}
    )
    average = round(sum(r.rating for r in reviews) / len(reviews), 2) if reviews else None
    return {
        "integration_id": integration_id,
        "rating": average,
        "review_count": len(reviews),
        "distribution": {
            str(score): sum(1 for r in reviews if r.rating == score)
            for score in range(MIN_RATING, MAX_RATING + 1)
        },
        "reviews": [_serialize_review(review, authors.get(review.user_id)) for review in reviews],
    }


@router.put("/listings/{integration_id}/reviews", status_code=200)
def upsert_review(
    integration_id: str,
    body: ReviewRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Record this person's rating. Re-submitting replaces it, never stacks."""
    ensure_agent_can(session, agent_auth, "logs:read")
    if marketplace.get_listing(session, agent_auth.org.id, integration_id) is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    review = session.exec(
        select(MarketplaceReview)
        .where(MarketplaceReview.org_id == agent_auth.org.id)
        .where(MarketplaceReview.user_id == current_user.id)
        .where(MarketplaceReview.integration_id == integration_id)
    ).first()
    first_time = review is None
    if review is None:
        review = MarketplaceReview(
            org_id=agent_auth.org.id,
            user_id=current_user.id,
            integration_id=integration_id,
            rating=body.rating,
        )
    review.rating = body.rating
    review.title = body.title
    review.body = body.body
    review.updated_at = datetime.utcnow()
    session.add(review)
    session.flush()

    # LLD §3.7's marketplace events. `review.created` fires once per person per
    # integration; `rating.updated` fires every time, because the aggregate has
    # moved whether the review is new or edited.
    if first_time:
        marketplace_events.review_created(
            session,
            org_id=agent_auth.org.id,
            integration_id=integration_id,
            review_id=review.id,
            rating=review.rating,
        )
    average, count = marketplace.rating_summary(session).get(
        integration_id, (float(review.rating), 1)
    )
    marketplace_events.rating_updated(
        session,
        org_id=agent_auth.org.id,
        integration_id=integration_id,
        average=average,
        review_count=count,
    )
    session.commit()
    session.refresh(review)
    return _serialize_review(review, current_user)


@router.delete("/listings/{integration_id}/reviews/{review_id}", status_code=204)
def delete_review(
    integration_id: str,
    review_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
    current_user: User = Depends(get_current_user),
) -> None:
    review = session.get(MarketplaceReview, review_id)
    if review is None or review.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="Review not found")
    # A review can be removed by its author, or by an org admin moderating.
    if review.user_id != current_user.id:
        ensure_agent_can(session, agent_auth, "org:settings:write")
    session.delete(review)
    session.commit()
