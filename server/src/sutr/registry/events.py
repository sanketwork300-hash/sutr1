"""The events the Registry publishes, and the Marketplace is built from.

LLD §3.7 names them:

    Registry     tool.registered · tool.updated · tool.deprecated ·
                 tool.archived · version.created
    Marketplace  tool.published · subscription.created · review.created ·
                 rating.updated · pricing.updated

The split in the LLD is by *which service the event is about*, not by which
service emits it — the marketplace is a projection and emits nothing of its
own. `tool.published` and `pricing.updated` are facts the Registry establishes
and the Marketplace reacts to, so they are published here; `subscription.created`,
`review.created` and `rating.updated` are storefront facts and live in
`marketplace/events.py` beside the code that causes them.

Every event is keyed on the tool id, because `events/topics.py` partitions
`tool.*` and `version.*` by `tool_id` and one tool's facts have to stay in
order: a `tool.deprecated` overtaking a `tool.updated` would leave the
projection describing a live tool that is not.

Every publish joins the caller's transaction, so nothing is announced for a
registration that later rolls back.
"""

import uuid
from typing import Any

from sqlmodel import Session

from sutr import events
from sutr.events import topics

PRODUCER = "registry"


def _publish(
    session: Session,
    event_type: str,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    **payload: Any,
) -> None:
    events.publish(
        session,
        event_type,
        tenant_id=org_id,
        resource_id=str(tool_id),
        producer=PRODUCER,
        payload={"tool_id": str(tool_id), **payload},
    )


def registered(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    tool_key: str,
    name: str,
    category: str,
    visibility: str,
) -> None:
    _publish(
        session,
        topics.TOOL_REGISTERED,
        org_id,
        tool_id,
        tool_key=tool_key,
        name=name,
        category=category,
        visibility=visibility,
    )


def updated(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    tool_key: str,
    changed: list[str],
    lifecycle_state: str,
    visibility: str,
) -> None:
    """Something about the tool changed.

    `changed` lists the field names rather than their values: a consumer needs
    to know whether to re-project, and an event carrying a tool's whole
    description would be a second copy of the record that can disagree with the
    first.
    """
    _publish(
        session,
        topics.TOOL_UPDATED,
        org_id,
        tool_id,
        tool_key=tool_key,
        changed=sorted(changed),
        lifecycle_state=lifecycle_state,
        visibility=visibility,
    )


def version_created(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    version: int,
    build_hash: str,
    artifact_id: uuid.UUID | None,
    tool_count: int,
) -> None:
    _publish(
        session,
        topics.VERSION_CREATED,
        org_id,
        tool_id,
        version=version,
        build_hash=build_hash,
        artifact_id=str(artifact_id) if artifact_id else None,
        tool_count=tool_count,
    )


def published(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    tool_key: str,
    version: int | None,
    visibility: str,
) -> None:
    _publish(
        session,
        topics.TOOL_PUBLISHED,
        org_id,
        tool_id,
        tool_key=tool_key,
        version=version,
        visibility=visibility,
    )


def approved(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    change_request_id: uuid.UUID,
    kind: str,
) -> None:
    """Governance allowed a gated change (the LLD's `tool.approved`)."""
    _publish(
        session,
        topics.TOOL_APPROVED,
        org_id,
        tool_id,
        change_request_id=str(change_request_id),
        kind=kind,
    )


def deprecated(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    tool_key: str,
    note: str,
) -> None:
    _publish(session, topics.TOOL_DEPRECATED, org_id, tool_id, tool_key=tool_key, note=note[:300])


def archived(session: Session, *, org_id: uuid.UUID, tool_id: uuid.UUID, tool_key: str) -> None:
    _publish(session, topics.TOOL_ARCHIVED, org_id, tool_id, tool_key=tool_key)


def pricing_updated(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID,
    pricing_id: uuid.UUID,
    model: str,
    amount_micros: int,
    currency: str,
    unit: str,
) -> None:
    _publish(
        session,
        topics.PRICING_UPDATED,
        org_id,
        tool_id,
        pricing_id=str(pricing_id),
        model=model,
        amount_micros=amount_micros,
        currency=currency,
        unit=unit,
    )
