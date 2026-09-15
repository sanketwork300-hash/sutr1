"""The events the storefront itself produces (LLD §3.7).

    subscription.created · review.created · rating.updated

The other three marketplace events the LLD names — `tool.published` and
`pricing.updated` — are facts the *Registry* establishes, so they are published
there. Splitting them by who causes the fact rather than by which chapter of
the LLD lists them is what keeps the projection a projection: everything it
consumes comes from upstream, and it emits only what happens in the storefront.
"""

import uuid
from typing import Any

from sqlmodel import Session

from sutr import events
from sutr.events import topics

PRODUCER = "marketplace"


def _publish(
    session: Session,
    event_type: str,
    org_id: uuid.UUID,
    tool_id: uuid.UUID | None,
    resource_id: str,
    **payload: Any,
) -> None:
    events.publish(
        session,
        event_type,
        tenant_id=org_id,
        resource_id=resource_id,
        producer=PRODUCER,
        payload={"tool_id": str(tool_id) if tool_id else None, **payload},
    )


def subscription_created(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    subscription_id: uuid.UUID,
    provider_org_id: uuid.UUID,
    pricing_id: uuid.UUID | None,
) -> None:
    """A tenant subscribed. Keyed on the tool, so a tool's facts stay ordered.

    `org_id` is the *consumer* — the tenant the event is about — while
    `provider_org_id` is in the payload. A provider reading their own tool's
    events therefore learns that somebody subscribed without the event being
    filed under their tenant, which would misattribute it.
    """
    _publish(
        session,
        topics.SUBSCRIPTION_CREATED,
        org_id,
        tool_id,
        str(tool_id),
        subscription_id=str(subscription_id),
        provider_org_id=str(provider_org_id),
        pricing_id=str(pricing_id) if pricing_id else None,
    )


def review_created(
    session: Session,
    *,
    org_id: uuid.UUID,
    integration_id: str,
    review_id: uuid.UUID,
    rating: int,
) -> None:
    _publish(
        session,
        topics.REVIEW_CREATED,
        org_id,
        None,
        integration_id,
        integration_id=integration_id,
        review_id=str(review_id),
        rating=rating,
    )


def rating_updated(
    session: Session,
    *,
    org_id: uuid.UUID,
    integration_id: str,
    average: float,
    review_count: int,
) -> None:
    _publish(
        session,
        topics.RATING_UPDATED,
        org_id,
        None,
        integration_id,
        integration_id=integration_id,
        average=round(average, 2),
        review_count=review_count,
    )
