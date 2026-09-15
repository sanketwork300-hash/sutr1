"""The Marketplace: the storefront, and the subscriptions bought through it.

This is the one surface in the platform that reads **across tenants** — that is
what a marketplace is. Two rules keep that from becoming a leak, and both are
in the query rather than in a filter afterwards:

- only listings in `published` state are returned, and
- only listings whose visibility is `public`, or whose provider is the caller's
  own organization.

Listings are a projection of the registry and can lag it. Every listing says
when it was projected and from which event, and **nothing authorizes off a
listing**: subscribing re-reads the registry record, so a tool archived two
events ago cannot be subscribed to because the storefront has not caught up.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import ConflictError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.marketplace import listings, profiles, projection, subscriptions
from sutr.models.registry_tool import RegistryTool
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/marketplace", tags=["marketplace"])

BROWSE = "logs:read"
SUBSCRIBE = "integrations:manage"


class SubscribeRequest(BaseModel):
    tool_id: uuid.UUID


class ProvisionRequest(BaseModel):
    # Set when provisioning did not work. Recorded rather than swallowed: a
    # subscription stuck in `provisioning` with no reason is worse than a
    # failed one that says why.
    failure: str = ""


class CancelRequest(BaseModel):
    reason: str = ""


class ProfileRequest(BaseModel):
    display_name: str | None = None
    summary: str | None = None
    website_url: str | None = None
    support_email: str | None = None
    support_url: str | None = None


@router.get("/listings")
def search_listings(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    tag: list[str] | None = Query(default=None),
    min_trust: int | None = Query(default=None, ge=0, le=100),
    provider_org_id: uuid.UUID | None = Query(default=None),
    sort: str = Query(default=listings.DEFAULT_SORT),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, BROWSE)
    rows = listings.search(
        session,
        viewer_org_id=agent_auth.org.id,
        query=q,
        category=category,
        tags=tag,
        min_trust=min_trust,
        provider_org_id=provider_org_id,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return envelope(
        {
            "listings": [listings.serialize(row) for row in rows],
            "sorts": list(listings.SORTS),
            "projection": projection.describe(),
        }
    )


@router.get("/categories")
def listing_categories(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, BROWSE)
    return envelope({"categories": listings.categories(session, viewer_org_id=agent_auth.org.id)})


@router.get("/listings/{tool_id}")
def get_listing(
    tool_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, BROWSE)
    listing = listings.get(session, viewer_org_id=agent_auth.org.id, tool_id=tool_id)
    if listing is None:
        # Unlisted and never-listed look the same from outside, which is the
        # point: a delisted tool must not be discoverable by 403.
        raise NotFoundError("That listing was not found.")
    subscription = subscriptions.open_subscription(
        session, org_id=agent_auth.org.id, tool_id=tool_id
    )
    return envelope(
        {
            **listings.serialize(listing, detail=True),
            "subscription": (
                subscriptions.serialize(subscription) if subscription is not None else None
            ),
        }
    )


@router.get("/providers/{org_id}")
def get_provider(
    org_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """A provider's public profile and the listings they have published."""
    ensure_agent_can(session, agent_auth, BROWSE)
    rows = listings.search(
        session, viewer_org_id=agent_auth.org.id, provider_org_id=org_id, limit=200
    )
    return envelope(
        {
            "provider": profiles.summary(session, org_id),
            "listings": [listings.serialize(row) for row in rows],
        }
    )


@router.get("/profile")
def get_own_profile(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, BROWSE)
    profile = profiles.get(session, agent_auth.org.id)
    if profile is None:
        return envelope({"profile": None, "defaults": profiles.summary(session, agent_auth.org.id)})
    return envelope({"profile": profiles.serialize(profile)})


@router.put("/profile")
def upsert_own_profile(
    body: ProfileRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Edit the provider profile. `verified` is absent by design.

    A verification badge a provider can set is a badge that means nothing, so
    it is granted by a platform administrator and never by this route.
    """
    ensure_agent_can(session, agent_auth, SUBSCRIBE)
    profile = profiles.upsert(session, agent_auth.org.id, body.model_dump(exclude_none=True))
    session.commit()
    session.refresh(profile)
    return envelope({"profile": profiles.serialize(profile)})


# ── Subscriptions (LLD §3.7: Discover → Subscribe → Provision → Use → Renew → Cancel)


@router.post("/subscriptions", status_code=201)
def subscribe(
    body: SubscribeRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Subscribe to a tool. Reads the registry, not the listing."""
    ensure_agent_can(session, agent_auth, SUBSCRIBE)
    tool = session.get(RegistryTool, body.tool_id)
    if tool is None:
        raise NotFoundError("That tool is not in the registry.")
    # A private tool is invisible, so "not found" is the honest answer rather
    # than "you may not subscribe" — which would confirm it exists.
    allowed, reason = subscriptions.subscribable(tool)
    if not allowed and tool.visibility == "private":
        raise NotFoundError("That tool is not in the registry.")
    if not allowed and tool.org_id != agent_auth.org.id:
        raise ConflictError(reason or "This tool cannot be subscribed to.")

    subscription = subscriptions.subscribe(
        session,
        org_id=agent_auth.org.id,
        tool=tool,
        subscribed_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="marketplace.subscribed",
        summary=f"Subscribed to '{tool.tool_key}'",
        target_type="registry_tool",
        target_id=str(tool.id),
        metadata={"subscription_id": str(subscription.id)},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(subscription)
    return envelope(subscriptions.serialize(subscription, tool=tool))


@router.get("/subscriptions")
def list_subscriptions(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, BROWSE)
    rows = subscriptions.list_for_org(session, agent_auth.org.id)
    return envelope({"subscriptions": [subscriptions.serialize(row) for row in rows]})


@router.get("/subscribers")
def list_subscribers(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Who subscribed to this organization's tools."""
    ensure_agent_can(session, agent_auth, BROWSE)
    rows = subscriptions.list_for_provider(session, agent_auth.org.id)
    return envelope({"subscribers": [subscriptions.serialize(row) for row in rows]})


def _load_subscription(session: Session, subscription_id: uuid.UUID, org_id: uuid.UUID):
    subscription = subscriptions.get(session, subscription_id, org_id)
    if subscription is None:
        raise NotFoundError("That subscription was not found.")
    return subscription


@router.post("/subscriptions/{subscription_id}/provision")
def provision_subscription(
    subscription_id: uuid.UUID,
    body: ProvisionRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, SUBSCRIBE)
    subscription = _load_subscription(session, subscription_id, agent_auth.org.id)
    subscriptions.provision(session, subscription, failure=body.failure)
    session.commit()
    session.refresh(subscription)
    return envelope(subscriptions.serialize(subscription))


@router.post("/subscriptions/{subscription_id}/renew")
def renew_subscription(
    subscription_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, SUBSCRIBE)
    subscription = _load_subscription(session, subscription_id, agent_auth.org.id)
    subscriptions.renew(session, subscription)
    session.commit()
    session.refresh(subscription)
    return envelope(subscriptions.serialize(subscription))


@router.post("/subscriptions/{subscription_id}/cancel")
def cancel_subscription(
    subscription_id: uuid.UUID,
    body: CancelRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, SUBSCRIBE)
    subscription = _load_subscription(session, subscription_id, agent_auth.org.id)
    subscriptions.cancel(session, subscription, reason=body.reason)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="marketplace.cancelled",
        summary="Subscription cancelled",
        target_type="registry_tool",
        target_id=str(subscription.tool_id),
        metadata={"subscription_id": str(subscription.id), "reason": body.reason[:200]},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(subscription)
    return envelope(subscriptions.serialize(subscription))
