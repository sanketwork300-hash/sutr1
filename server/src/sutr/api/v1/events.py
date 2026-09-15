"""The event log: what the platform has announced, and what it failed to.

Every control-plane stage emits a fact, and when something does not happen the
first question is always "did the event get out?". Without a way to look, the
answer is a database query someone runs by hand. This is that view, plus the
manual-investigation step the LLD's retry → DLQ → manual chain ends with
(§5.6).

Scoped to the caller's organization. Events with no tenant are platform-wide
facts and are visible to any authenticated caller of the instance; nothing here
exposes another tenant's events.
"""

import json
import uuid

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, or_, select

from sutr.authz import ensure_agent_can
from sutr.common import Page, PageRequest, envelope
from sutr.common.errors import NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.events import outbox
from sutr.models.outbox_event import DEAD_LETTERED, FAILED, PENDING, PUBLISHED, OutboxEvent

router = APIRouter(prefix="/v1/events", tags=["events"])

STATES = (PENDING, PUBLISHED, FAILED, DEAD_LETTERED)


def _serialize(row: OutboxEvent) -> dict:
    return {
        "event_id": str(row.event_id),
        "event_type": row.event_type,
        "event_version": row.event_version,
        "partition_key": row.partition_key,
        "correlation_id": row.correlation_id,
        "tenant_id": row.tenant_id,
        "resource_id": row.resource_id,
        "producer": row.producer,
        "state": row.state,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "payload": json.loads(row.envelope_json).get("payload", {}),
        "created_at": row.created_at.isoformat(),
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


def _visible(statement, org_id: uuid.UUID):
    """Restrict to this tenant's events, plus platform-wide ones."""
    return statement.where(
        or_(col(OutboxEvent.tenant_id) == str(org_id), col(OutboxEvent.tenant_id).is_(None))
    )


@router.get("")
def list_events(
    event_type: str | None = None,
    state: str | None = None,
    correlation_id: str | None = None,
    limit: int | None = Query(default=None),
    cursor: str | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Recent events, newest first, cursor-paginated (LLD §5.5)."""
    ensure_agent_can(session, agent_auth, "audit:read")
    page_request = PageRequest.parse(limit=limit, cursor=cursor)

    statement = _visible(select(OutboxEvent), agent_auth.org.id)
    if event_type:
        statement = statement.where(OutboxEvent.event_type == event_type)
    if state:
        statement = statement.where(OutboxEvent.state == state)
    if correlation_id:
        statement = statement.where(OutboxEvent.correlation_id == correlation_id)
    if page_request.cursor and "id" in page_request.cursor:
        # Newest first, so the next page is everything *older* than the cursor.
        statement = statement.where(col(OutboxEvent.id) < int(page_request.cursor["id"]))

    rows = list(
        session.exec(
            statement.order_by(col(OutboxEvent.id).desc()).limit(page_request.fetch_limit)
        ).all()
    )
    page = Page.build(rows, page_request, cursor_for=lambda row: {"id": row.id})
    return envelope(
        [_serialize(row) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/dead-letter")
def list_dead_letter(
    limit: int | None = Query(default=None),
    cursor: str | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Events the relay gave up on. The LLD's "manual investigation" queue."""
    ensure_agent_can(session, agent_auth, "audit:read")
    page_request = PageRequest.parse(limit=limit, cursor=cursor)
    statement = _visible(
        select(OutboxEvent).where(OutboxEvent.state == DEAD_LETTERED), agent_auth.org.id
    )
    if page_request.cursor and "id" in page_request.cursor:
        statement = statement.where(col(OutboxEvent.id) < int(page_request.cursor["id"]))
    rows = list(
        session.exec(
            statement.order_by(col(OutboxEvent.id).desc()).limit(page_request.fetch_limit)
        ).all()
    )
    page = Page.build(rows, page_request, cursor_for=lambda row: {"id": row.id})
    return envelope(
        [_serialize(row) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/{event_id}/retry")
def retry_event(
    event_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Return a failed or dead-lettered event to the queue.

    Idempotent by nature rather than by key: an event already pending or
    published is returned unchanged, because "make this event pending" has the
    same result however many times it is asked.
    """
    ensure_agent_can(session, agent_auth, "org:settings:write")
    row = session.exec(
        _visible(select(OutboxEvent).where(OutboxEvent.event_id == event_id), agent_auth.org.id)
    ).first()
    if row is None:
        raise NotFoundError(f"No event with id {event_id}.")
    if row.state in (FAILED, DEAD_LETTERED):
        outbox.retry(session, row)
        session.commit()
        session.refresh(row)
    return envelope(_serialize(row))
