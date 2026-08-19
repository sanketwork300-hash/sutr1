import logging
from datetime import datetime
from typing import Literal

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from sutr.billing.webhook import handle_event
from sutr.config import settings
from sutr.db import get_session
from sutr.dependencies import get_current_org, get_current_user
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.processed_stripe_event import ProcessedStripeEvent
from sutr.models.subscription import Subscription
from sutr.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])


class SubscriptionResponse(BaseModel):
    tier: Literal["free", "plus"]
    status: str
    current_period_end: datetime | None
    cancel_at_period_end: bool
    enterprise_contact_email: str


class RedirectResponse(BaseModel):
    url: str


def _billing_or_404() -> None:
    if not settings.billing_enabled():
        raise HTTPException(status_code=404, detail="billing_disabled")


def _configure_stripe() -> None:
    stripe.api_key = settings.stripe_api_key


def _require_owner(user: User, org: Org, session: Session) -> None:
    membership = session.get(OrgMembership, (user.id, org.id))
    if membership is None or membership.role != "owner":
        raise HTTPException(status_code=403, detail="owner_required")


def _get_or_create_subscription(org: Org, session: Session) -> Subscription:
    sub = session.get(Subscription, org.id)
    if sub is None:
        _configure_stripe()
        customer = stripe.Customer.create(
            name=org.name,
            metadata={"org_id": str(org.id)},
        )
        sub = Subscription(
            org_id=org.id,
            stripe_customer_id=customer.id,
            tier="free",
            status="active",
        )
        session.add(sub)
        session.commit()
        session.refresh(sub)
    return sub


@router.get("/subscription")
def get_subscription(
    session: Session = Depends(get_session),
    current_org: Org = Depends(get_current_org),
) -> SubscriptionResponse:
    _billing_or_404()
    sub = _get_or_create_subscription(current_org, session)
    return SubscriptionResponse(
        tier=sub.tier,  # type: ignore[arg-type]
        status=sub.status,
        current_period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        enterprise_contact_email=settings.enterprise_contact_email,
    )


@router.post("/checkout")
def create_checkout_session(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
) -> RedirectResponse:
    _billing_or_404()
    _require_owner(current_user, current_org, session)
    _configure_stripe()

    sub = _get_or_create_subscription(current_org, session)

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        customer=sub.stripe_customer_id,
        client_reference_id=str(current_org.id),
        line_items=[{"price": settings.stripe_price_plus, "quantity": 1}],
        success_url=f"{settings.ui_base_url}/settings/billing?checkout=success",
        cancel_url=f"{settings.ui_base_url}/settings/billing?checkout=cancel",
        subscription_data={"metadata": {"org_id": str(current_org.id)}},
    )
    if checkout.url is None:
        raise HTTPException(status_code=500, detail="stripe_missing_url")
    return RedirectResponse(url=checkout.url)


@router.post("/portal")
def create_portal_session(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
) -> RedirectResponse:
    _billing_or_404()
    _require_owner(current_user, current_org, session)
    _configure_stripe()

    sub = _get_or_create_subscription(current_org, session)
    portal = stripe.billing_portal.Session.create(
        customer=sub.stripe_customer_id,
        return_url=f"{settings.ui_base_url}/settings/billing",
    )
    return RedirectResponse(url=portal.url)


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _billing_or_404()
    _configure_stripe()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=settings.stripe_webhook_secret,
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="invalid_signature") from exc

    event_id = event["id"] if "id" in event else None
    event_type = event["type"] if "type" in event else ""

    # Stripe retries until it gets a 2xx, so the same event arrives more than
    # once in normal operation. Claim the id first: the insert is the lock, and
    # committing it in the same transaction as the handler's writes means a
    # failed handler rolls the claim back and the retry is still processed.
    if event_id:
        if session.get(ProcessedStripeEvent, event_id) is not None:
            logger.info("Ignoring already-processed Stripe event %s (%s)", event_id, event_type)
            return {"received": True, "duplicate": True}
        session.add(ProcessedStripeEvent(event_id=event_id, event_type=event_type))

    try:
        handle_event(event, session)
        session.commit()
    except IntegrityError:
        # A concurrent delivery of the same event won the race.
        session.rollback()
        logger.info("Concurrent delivery of Stripe event %s ignored", event_id)
        return {"received": True, "duplicate": True}
    except Exception:
        session.rollback()
        logger.exception("Failed to handle Stripe event %s", event_id)
        raise HTTPException(status_code=500, detail="handler_error")
    return {"received": True}
