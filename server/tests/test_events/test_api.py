"""The /v1/events surface: who may retry what, and what a bad cursor costs."""

from sqlmodel import select

from sutr.common.pagination import encode_cursor
from sutr.events import outbox, topics
from sutr.models.outbox_event import DEAD_LETTERED, PENDING, OutboxEvent


def _dead_letter(session, tenant_id):
    outbox.publish(session, topics.API_UPLOADED, tenant_id=tenant_id, resource_id="p-1")
    session.commit()
    row = session.exec(select(OutboxEvent)).one()
    row.state = DEAD_LETTERED
    row.attempts = 5
    session.add(row)
    session.commit()
    return row


async def test_a_tenant_cannot_retry_a_platform_wide_event(client, session, test_user):
    """A platform-wide event is visible to every tenant and belongs to none;
    re-queueing it re-runs its consumers for the whole instance."""
    row = _dead_letter(session, None)
    assert test_user.is_admin is False
    assert len((await client.get("/v1/events/dead-letter")).json()["data"]) == 1

    response = await client.post(f"/v1/events/{row.event_id}/retry")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    session.refresh(row)
    assert row.state == DEAD_LETTERED


async def test_a_platform_administrator_can_retry_a_platform_wide_event(client, session, test_user):
    row = _dead_letter(session, None)
    test_user.is_admin = True
    response = await client.post(f"/v1/events/{row.event_id}/retry")
    assert response.status_code == 200
    assert response.json()["data"]["state"] == PENDING


async def test_a_tenant_still_retries_its_own_events(client, session, test_org, test_user):
    row = _dead_letter(session, test_org.id)
    assert test_user.is_admin is False
    response = await client.post(f"/v1/events/{row.event_id}/retry")
    assert response.status_code == 200
    assert response.json()["data"]["state"] == PENDING


async def test_a_cursor_whose_id_is_not_a_number_is_a_400_not_a_500(client):
    """L16: the cursor is caller-supplied; a value that decodes but carries no
    integer id is the caller's mistake and is named as such."""
    cursor = encode_cursor({"id": "not-a-number"})
    for path in ("/v1/events", "/v1/events/dead-letter"):
        response = await client.get(path, params={"cursor": cursor})
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == "invalid_request"
