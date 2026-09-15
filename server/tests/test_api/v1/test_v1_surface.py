"""The `/v1` surface: envelope, cursor pagination, errors, idempotency.

ADR-004 splits the two surfaces: `/api` is frozen for the published CLI and
SDKs, `/v1` is where the LLD's API standards apply. These tests hold both ends
of that — the new surface follows the standards, and the old one is unchanged.
"""

import json

import pytest
from sqlmodel import select

from sutr.events import outbox, topics
from sutr.models.outbox_event import DEAD_LETTERED, PENDING, OutboxEvent

# ── The envelope ─────────────────────────────────────────────────────────────


async def test_a_v1_response_is_enveloped(client):
    resp = await client.get("/v1/platform/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert isinstance(body["meta"], dict)


async def test_the_envelope_carries_the_correlation_id(client):
    resp = await client.get(
        "/v1/platform/capabilities", headers={"X-Correlation-Id": "corr-from-caller"}
    )
    assert resp.json()["meta"]["correlation_id"] == "corr-from-caller"
    assert resp.headers["X-Correlation-ID"] == "corr-from-caller"


async def test_the_api_surface_is_still_bare_json(client):
    """ADR-004: `/api` must not grow an envelope — the published CLI reads it."""
    resp = await client.get("/api/quotas")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list), "/api responses must stay unenveloped"


# ── Capabilities ─────────────────────────────────────────────────────────────


async def test_capabilities_report_what_is_degraded_and_why(client):
    body = (await client.get("/v1/platform/capabilities")).json()["data"]
    names = {entry["name"] for entry in body["capabilities"]}
    assert {"event_bus", "secrets_kms", "tracing", "metrics", "mcp_sse"} <= names

    for entry in body["capabilities"]:
        if entry["available"]:
            assert entry["reason"] is None, "a working capability must not carry a warning"
        else:
            assert entry["reason"], f"{entry['name']} is unavailable without saying why"
    assert body["degraded"] == [
        entry["name"] for entry in body["capabilities"] if not entry["available"]
    ]


async def test_the_event_bus_is_reported_as_available_by_default(client):
    body = (await client.get("/v1/platform/capabilities")).json()["data"]
    bus = next(entry for entry in body["capabilities"] if entry["name"] == "event_bus")
    assert bus["available"] is True
    assert "in_process" in bus["detail"]


async def test_the_service_map_is_served(client):
    body = (await client.get("/v1/platform/services")).json()["data"]
    assert set(body["planes"]) == {"control", "data", "shared"}
    assert body["service_count"] > 10
    control = {service["name"] for service in body["planes"]["control"]}
    assert "governance" in control


# ── The event log ────────────────────────────────────────────────────────────


@pytest.fixture(name="some_events")
def some_events_fixture(session, test_org):
    for index in range(5):
        outbox.publish(
            session,
            topics.API_UPLOADED,
            tenant_id=test_org.id,
            resource_id=f"project-{index}",
            payload={"index": index},
        )
    outbox.publish(session, topics.TOOL_PUBLISHED, tenant_id=test_org.id, resource_id="tool-1")
    session.commit()


async def test_events_are_listed_newest_first(client, some_events):
    body = (await client.get("/v1/events")).json()
    assert len(body["data"]) == 6
    assert body["data"][0]["event_type"] == topics.TOOL_PUBLISHED


async def test_events_paginate_by_cursor(client, some_events):
    first = (await client.get("/v1/events?limit=2")).json()
    assert len(first["data"]) == 2
    cursor = first["meta"]["next_cursor"]
    assert cursor

    second = (await client.get(f"/v1/events?limit=2&cursor={cursor}")).json()
    assert len(second["data"]) == 2
    first_ids = {row["event_id"] for row in first["data"]}
    second_ids = {row["event_id"] for row in second["data"]}
    assert not (first_ids & second_ids), "pages must not overlap"


async def test_the_last_page_carries_no_cursor(client, some_events):
    body = (await client.get("/v1/events?limit=100")).json()
    assert "next_cursor" not in body["meta"]


async def test_a_forged_cursor_is_a_400_in_the_standard_error_shape(client):
    resp = await client.get("/v1/events?cursor=not-a-cursor!!")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"]["parameter"] == "cursor"
    assert body["meta"]["request_id"]


async def test_events_can_be_filtered(client, some_events):
    by_type = (await client.get(f"/v1/events?event_type={topics.TOOL_PUBLISHED}")).json()
    assert len(by_type["data"]) == 1

    by_state = (await client.get(f"/v1/events?state={PENDING}")).json()
    assert len(by_state["data"]) == 6


async def test_events_can_be_traced_by_correlation_id(client, session, test_org):
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(_PETSTORE)},
        headers={"X-Correlation-Id": "trace-me"},
    )
    assert resp.status_code == 201
    body = (await client.get("/v1/events?correlation_id=trace-me")).json()
    types = {row["event_type"] for row in body["data"]}
    assert {topics.API_UPLOADED, topics.TRANSLATION_COMPLETED} <= types
    assert all(row["correlation_id"] == "trace-me" for row in body["data"])


async def test_one_tenants_events_are_not_visible_to_another(client, session, test_org):
    import uuid

    from sutr.models.org import Org

    other = Org(id=uuid.uuid4(), name="Other")
    session.add(other)
    session.commit()
    outbox.publish(session, topics.TOOL_PUBLISHED, tenant_id=other.id, resource_id="their-tool")
    session.commit()

    body = (await client.get("/v1/events")).json()
    assert all(row["tenant_id"] != str(other.id) for row in body["data"])


async def test_a_dead_lettered_event_can_be_retried(client, session, test_org):
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()
    row = session.exec(select(OutboxEvent)).one()
    row.state = DEAD_LETTERED
    row.attempts = 5
    session.add(row)
    session.commit()

    listed = (await client.get("/v1/events/dead-letter")).json()
    assert len(listed["data"]) == 1

    retried = await client.post(f"/v1/events/{row.event_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["data"]["state"] == PENDING
    assert retried.json()["data"]["attempts"] == 0


async def test_retrying_an_already_pending_event_changes_nothing(client, session, test_org):
    outbox.publish(session, topics.API_UPLOADED, tenant_id=test_org.id, resource_id="p-1")
    session.commit()
    row = session.exec(select(OutboxEvent)).one()
    resp = await client.post(f"/v1/events/{row.event_id}/retry")
    assert resp.status_code == 200
    assert resp.json()["data"]["state"] == PENDING


async def test_retrying_an_unknown_event_is_a_standard_404(client):
    import uuid

    resp = await client.post(f"/v1/events/{uuid.uuid4()}/retry")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


_PETSTORE = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://api.petstore.example.com/v1"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


# ── Events are actually emitted by the pipeline ──────────────────────────────


async def test_importing_a_specification_announces_it(client, session, test_org):
    await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(_PETSTORE)},
    )
    rows = session.exec(select(OutboxEvent)).all()
    types = {row.event_type for row in rows}
    assert topics.API_UPLOADED in types
    assert topics.TRANSLATION_COMPLETED in types


async def test_a_rejected_import_announces_nothing(client, session):
    """The event and the state change commit together, or not at all."""
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps({"openapi": "3.0.0", "info": {}})},
    )
    assert resp.status_code == 400
    assert session.exec(select(OutboxEvent)).all() == []


async def test_compiling_announces_generation_and_registration(client, session, monkeypatch):
    monkeypatch.setattr(
        "sutr.api.custom_api.validate_safe_url", lambda url, allow_query=False: None
    )
    created = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(_PETSTORE)},
    )
    await client.post(f"/api/openapi/{created.json()['id']}/compile", json={})
    types = {row.event_type for row in session.exec(select(OutboxEvent)).all()}
    assert topics.MCP_GENERATED in types
    assert topics.TOOL_REGISTERED in types
