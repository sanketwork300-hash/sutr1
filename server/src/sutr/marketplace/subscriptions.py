"""The subscription lifecycle: Discover → Subscribe → Provision → Use → Renew → Cancel.

LLD §3.7 names those six steps. Five of them are states here; **Discover is
not**, because discovery is what happens before a subscription exists and a
state for "has not subscribed" would be a row for every tenant that ever
browsed.

Three rules the code below enforces, each of which is a decision:

**Subscribing re-reads the registry.** Not the listing. A listing is a
projection that can lag, and a tenant must not be able to subscribe to a tool
that was archived two events ago because the storefront has not caught up.

**A tenant holds one open subscription per tool.** Subscribing twice is a
mistake, not a second entitlement. Cancelled subscriptions are kept and
re-subscribing writes a new row, so "who had access in March" stays answerable.

**Provisioning can fail, and says so.** A subscription stuck in `provisioning`
forever would be a lie of omission; the failure state carries the reason.

Nothing here charges anybody. The price is snapshotted by id so a later price
change does not silently reprice an existing subscriber, but no money moves —
that is billing's work, and this is the metadata it would read (ADR-046).
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.common.errors import ConflictError, InvalidRequestError, NotFoundError
from sutr.marketplace import events as marketplace_events
from sutr.models.registry_tool import (
    ACTIVE,
    ARCHIVED,
    PUBLISHED,
    VISIBILITY_PRIVATE,
    RegistryTool,
)
from sutr.models.tool_subscription import (
    OPEN_STATES,
    STATE_ACTIVE,
    STATE_CANCELLED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_PROVISIONING,
    STATE_SUBSCRIBED,
    ToolSubscription,
)
from sutr.registry import pricing

# States a tool must be in to accept new subscribers. A DEPRECATED tool keeps
# its existing subscribers — that is what deprecation means — but does not take
# new ones.
SUBSCRIBABLE_STATES = (PUBLISHED, ACTIVE)

# How long a subscription runs before it needs renewing. A month, because the
# only priced unit that is not per-call is per-month.
TERM_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Treat a stored datetime as UTC.

    Timestamps go into SQLite aware and come back naive, and comparing a naive
    datetime with an aware one raises `TypeError`. That comparison is on the
    renewal path, so without this it would work in a test that never reloads
    the row and fail the first time a real request renewed a subscription.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def open_subscription(
    session: Session, *, org_id: uuid.UUID, tool_id: uuid.UUID
) -> ToolSubscription | None:
    return session.exec(
        select(ToolSubscription)
        .where(ToolSubscription.org_id == org_id)
        .where(ToolSubscription.tool_id == tool_id)
        .where(col(ToolSubscription.state).in_(OPEN_STATES))
    ).first()


def get(session: Session, subscription_id: uuid.UUID, org_id: uuid.UUID) -> ToolSubscription | None:
    subscription = session.get(ToolSubscription, subscription_id)
    if subscription is None or subscription.org_id != org_id:
        # A provider looking at a subscriber's row goes through
        # `list_for_provider`; this lookup is the consumer's own.
        return None
    return subscription


def list_for_org(session: Session, org_id: uuid.UUID) -> list[ToolSubscription]:
    return list(
        session.exec(
            select(ToolSubscription)
            .where(ToolSubscription.org_id == org_id)
            .order_by(desc(col(ToolSubscription.subscribed_at)))
        ).all()
    )


def list_for_provider(session: Session, provider_org_id: uuid.UUID) -> list[ToolSubscription]:
    """Who subscribed to this provider's tools.

    A provider sees that a subscription exists and what it is for. They do not
    get the consumer's rows — `provider_org_id` is denormalized precisely so
    this query never has to read across a tenant boundary.
    """
    return list(
        session.exec(
            select(ToolSubscription)
            .where(ToolSubscription.provider_org_id == provider_org_id)
            .order_by(desc(col(ToolSubscription.subscribed_at)))
        ).all()
    )


def subscribable(tool: RegistryTool) -> tuple[bool, str | None]:
    """Whether a tool can be subscribed to, and why not when it cannot."""
    if tool.visibility == VISIBILITY_PRIVATE:
        return False, "This tool is private to its provider."
    if tool.lifecycle_state == ARCHIVED:
        return False, "This tool has been archived."
    if tool.lifecycle_state not in SUBSCRIBABLE_STATES:
        return False, (
            f"This tool is {tool.lifecycle_state}, not published, so it is not taking "
            "subscribers yet."
        )
    return True, None


def subscribe(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool: RegistryTool,
    subscribed_by_user_id: uuid.UUID | None = None,
) -> ToolSubscription:
    """Subscribe. The caller commits."""
    allowed, reason = subscribable(tool)
    if not allowed:
        raise ConflictError(reason or "This tool cannot be subscribed to.")
    if org_id == tool.org_id:
        raise ConflictError(
            "A provider already has access to their own tool; subscribing to it would create a "
            "subscriber count and a bill that mean nothing."
        )
    if open_subscription(session, org_id=org_id, tool_id=tool.id) is not None:
        raise ConflictError("This organization is already subscribed to this tool.")

    live_price = pricing.current(session, tool.id)
    subscription = ToolSubscription(
        org_id=org_id,
        tool_id=tool.id,
        provider_org_id=tool.org_id,
        state=STATE_SUBSCRIBED,
        pricing_id=live_price.id if live_price else None,
        version=tool.published_version,
        subscribed_by_user_id=subscribed_by_user_id,
    )
    session.add(subscription)
    session.flush()
    marketplace_events.subscription_created(
        session,
        org_id=org_id,
        tool_id=tool.id,
        subscription_id=subscription.id,
        provider_org_id=tool.org_id,
        pricing_id=live_price.id if live_price else None,
    )
    return subscription


def provision(
    session: Session, subscription: ToolSubscription, *, failure: str = ""
) -> ToolSubscription:
    """Move from Subscribe to Provision, and on to Use — or to failed.

    Provisioning here is bookkeeping: it records that the tenant's access to
    this tool has been arranged. What *arranging* means depends on the tool —
    a deployment, a credential, nothing at all — and this platform does not
    perform it as a side effect of a subscription. Recording it as done when
    nothing happened would be the fake completion §83 forbids, so the step
    exists, is explicit, and is honest about being a state change.
    """
    if subscription.state not in (STATE_SUBSCRIBED, STATE_PROVISIONING):
        raise ConflictError(
            f"A {subscription.state} subscription is not waiting to be provisioned."
        )
    now = _utcnow()
    if failure:
        subscription.state = STATE_FAILED
        subscription.failure_reason = failure
    else:
        subscription.state = STATE_ACTIVE
        subscription.provisioned_at = now
        subscription.renews_at = now + timedelta(days=TERM_DAYS)
    subscription.updated_at = now
    session.add(subscription)
    return subscription


def renew(session: Session, subscription: ToolSubscription) -> ToolSubscription:
    if subscription.state != STATE_ACTIVE:
        raise ConflictError(f"A {subscription.state} subscription cannot be renewed.")
    now = _utcnow()
    # Extended from the existing expiry when it is still in the future, so a
    # tenant renewing early is not penalised by losing the remaining days.
    current = as_utc(subscription.renews_at)
    base = current if current is not None and current > now else now
    subscription.renews_at = base + timedelta(days=TERM_DAYS)
    subscription.renewed_count += 1
    subscription.updated_at = now
    session.add(subscription)
    return subscription


def cancel(
    session: Session, subscription: ToolSubscription, *, reason: str = ""
) -> ToolSubscription:
    if subscription.state in (STATE_CANCELLED, STATE_EXPIRED):
        raise ConflictError(f"This subscription is already {subscription.state}.")
    subscription.state = STATE_CANCELLED
    subscription.cancelled_at = _utcnow()
    subscription.cancel_reason = reason
    subscription.updated_at = subscription.cancelled_at
    session.add(subscription)
    return subscription


def expire_due(session: Session, *, now: datetime | None = None) -> int:
    """Expire active subscriptions past their renewal date. Returns how many.

    Separate from `renew` and driven by a caller rather than by a clock inside
    the model, because an entitlement that lapses silently at read time is an
    entitlement whose end nobody can point at.
    """
    moment = as_utc(now) or _utcnow()
    due = session.exec(
        select(ToolSubscription)
        .where(ToolSubscription.state == STATE_ACTIVE)
        .where(col(ToolSubscription.renews_at).is_not(None))
        .where(col(ToolSubscription.renews_at) < moment)
    ).all()
    for subscription in due:
        subscription.state = STATE_EXPIRED
        subscription.updated_at = moment
        session.add(subscription)
    return len(due)


def entitled(session: Session, *, org_id: uuid.UUID, tool_id: uuid.UUID) -> bool:
    """Does this tenant currently have access? Read from the registry's side.

    The one question the whole table exists to answer, and the reason it is not
    answered from a listing.
    """
    subscription = session.exec(
        select(ToolSubscription)
        .where(ToolSubscription.org_id == org_id)
        .where(ToolSubscription.tool_id == tool_id)
        .where(ToolSubscription.state == STATE_ACTIVE)
    ).first()
    return subscription is not None


def load_tool(session: Session, tool_id: uuid.UUID) -> RegistryTool:
    """The registry record, read fresh. Raises if it is gone."""
    tool = session.get(RegistryTool, tool_id)
    if tool is None:
        raise NotFoundError("That tool is not in the registry.")
    return tool


def serialize(
    subscription: ToolSubscription, *, tool: RegistryTool | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(subscription.id),
        "tool_id": str(subscription.tool_id),
        "org_id": str(subscription.org_id),
        "provider_org_id": str(subscription.provider_org_id),
        "state": subscription.state,
        "pricing_id": str(subscription.pricing_id) if subscription.pricing_id else None,
        "version": subscription.version,
        "subscribed_at": subscription.subscribed_at.isoformat(),
        "provisioned_at": (
            subscription.provisioned_at.isoformat() if subscription.provisioned_at else None
        ),
        "renews_at": subscription.renews_at.isoformat() if subscription.renews_at else None,
        "renewed_count": subscription.renewed_count,
        "cancelled_at": (
            subscription.cancelled_at.isoformat() if subscription.cancelled_at else None
        ),
        "cancel_reason": subscription.cancel_reason or None,
        "failure_reason": subscription.failure_reason or None,
        "entitled": subscription.state == STATE_ACTIVE,
        "billed_by_this_platform": False,
    }
    if tool is not None:
        payload["tool"] = {"tool_key": tool.tool_key, "name": tool.name}
    return payload


def raise_if_unknown_state(state: str) -> None:
    if state not in (
        STATE_SUBSCRIBED,
        STATE_PROVISIONING,
        STATE_ACTIVE,
        STATE_CANCELLED,
        STATE_EXPIRED,
        STATE_FAILED,
    ):
        raise InvalidRequestError(f"Unknown subscription state '{state}'.")
