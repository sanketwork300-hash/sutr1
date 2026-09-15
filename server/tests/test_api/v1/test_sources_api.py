"""The /v1/sources surface: attach, watch, check, compare, decide.

Applying a drift report is an explicit action on purpose. Build prompt §13
requires that breaking changes not deploy automatically unless policy says so —
which means there must be a place where a human says so, and a record that they
did.
"""

import json
from unittest.mock import patch

import pytest
from sqlmodel import select

from sutr.models.api_source import ApiSource
from sutr.models.drift_report import STATUS_APPLIED, STATUS_DISMISSED, DriftReport
from sutr.models.outbox_event import OutboxEvent
from sutr.source_connectors.base import ConnectionResult, FetchResult, Provenance

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/pets/{id}": {
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete.",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"204": {"description": "gone"}},
            }
        },
    },
}


def _spec_text(remove_delete: bool = False) -> str:
    document = json.loads(json.dumps(SPEC))
    if remove_delete:
        del document["paths"]["/pets/{id}"]
    return json.dumps(document)


def _url_connector(content: str):
    """Patch the URL connector so no network is touched."""

    async def fake_connect(self, config, secrets):
        return ConnectionResult(connected=True, message="Found Pets 1.0.0.")

    async def fake_fetch(self, config, secrets, *, known=None):
        return FetchResult(
            content=content,
            provenance=Provenance(source_type="url", source_uri=config.get("url", ""), etag='"v1"'),
        )

    return (
        patch("sutr.source_connectors.url.UrlConnector.connect", fake_connect),
        patch("sutr.source_connectors.url.UrlConnector.fetch", fake_fetch),
    )


@pytest.fixture(name="project_id")
async def project_id_fixture(client):
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": _spec_text()},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _attach(client, project_id, content=None, **overrides):
    connect, fetch = _url_connector(content or _spec_text())
    with connect, fetch:
        return await client.post(
            "/v1/sources",
            json={
                "project_id": project_id,
                "connector": "url",
                "config": {"url": "https://api.example.com/openapi.json"},
                **overrides,
            },
        )


# ── Connectors ───────────────────────────────────────────────────────────────


async def test_connectors_are_listed_with_the_planned_ones(client):
    body = (await client.get("/v1/sources/connectors")).json()["data"]
    ids = {entry["id"] for entry in body["connectors"]}
    assert {"paste", "upload", "url", "swagger_ui", "github", "swaggerhub", "postman"} <= ids
    planned = {entry["id"] for entry in body["planned"]}
    assert {"wsdl", "api_gateway"} <= planned
    assert all(entry["available"] is False for entry in body["planned"])


async def test_a_planned_connector_is_refused_with_its_reason(client, project_id):
    resp = await client.post(
        "/v1/sources",
        json={"project_id": project_id, "connector": "wsdl", "config": {}},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "connector_not_implemented"
    assert "Zeep" in error["message"] or "WSDL" in error["message"]


async def test_an_unknown_connector_lists_the_available_ones(client, project_id):
    resp = await client.post(
        "/v1/sources",
        json={"project_id": project_id, "connector": "carrier_pigeon", "config": {}},
    )
    assert resp.status_code == 400
    assert "url" in resp.json()["error"]["details"]["available"]


# ── Validation as a preview of the import ────────────────────────────────────


async def test_validate_reports_every_pipeline_stage(client):
    resp = await client.post("/v1/sources/validate", json={"content": _spec_text()})
    body = resp.json()["data"]
    assert body["ok"] is True
    names = [stage["name"] for stage in body["stages"]]
    assert names == ["parse", "convert", "validate", "lint", "resolve", "normalize"]
    assert body["ir_hash"] and body["ir_version"]


async def test_validate_says_which_stage_failed(client):
    resp = await client.post("/v1/sources/validate", json={"content": "{not json"})
    body = resp.json()["data"]
    assert body["ok"] is False
    assert body["failed_stage"] == "parse"
    assert body["error_code"] == "parse_error"


async def test_validate_halts_before_the_ir_when_the_document_is_invalid(client):
    resp = await client.post(
        "/v1/sources/validate",
        json={"content": json.dumps({"openapi": "3.0.0", "info": {}, "paths": {}})},
    )
    body = resp.json()["data"]
    assert body["failed_stage"] == "validate"
    assert body["ir_hash"] is None
    assert "normalize" not in [stage["name"] for stage in body["stages"]]


async def test_validate_shows_a_swagger_2_conversion(client):
    swagger2 = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "1.0"},
        "host": "api.example.com",
        "basePath": "/v2",
        "schemes": ["https"],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    body = (
        await client.post("/v1/sources/validate", json={"content": json.dumps(swagger2)})
    ).json()["data"]
    convert = next(stage for stage in body["stages"] if stage["name"] == "convert")
    assert convert["status"] == "ok"
    assert convert["detail"]["from"] == "swagger2"


# ── Attaching a source ───────────────────────────────────────────────────────


async def test_attaching_a_source_checks_it_is_reachable_first(client, project_id):
    resp = await _attach(client, project_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["connector"] == "url"
    assert body["role"] == "primary"
    assert body["provenance"]["ir_hash"], "the baseline was populated"
    assert body["watch"]["plan"]["conditional"] is True


async def test_an_unreachable_source_is_refused_before_it_is_stored(client, project_id, session):
    async def failing_connect(self, config, secrets):
        return ConnectionResult(connected=False, message="The URL returned HTTP 404.")

    with patch("sutr.source_connectors.url.UrlConnector.connect", failing_connect):
        resp = await client.post(
            "/v1/sources",
            json={
                "project_id": project_id,
                "connector": "url",
                "config": {"url": "https://api.example.com/nope.json"},
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "source_unreachable"
    assert session.exec(select(ApiSource)).all() == []


async def test_a_source_that_cannot_be_watched_refuses_to_be(client, project_id):
    resp = await client.post(
        "/v1/sources",
        json={
            "project_id": project_id,
            "connector": "paste",
            "config": {"content": _spec_text()},
            "watch_enabled": True,
        },
    )
    assert resp.status_code == 400
    assert "cannot be watched" in resp.json()["error"]["message"]


async def test_attaching_announces_source_connected(client, project_id, session):
    await _attach(client, project_id)
    from sutr.events import topics

    types = {row.event_type for row in session.exec(select(OutboxEvent)).all()}
    assert topics.SOURCE_CONNECTED in types


async def test_a_source_never_returns_its_token(client, project_id):
    resp = await _attach(client, project_id, token="ghp_secret_value")
    assert resp.status_code == 201
    assert "ghp_secret_value" not in resp.text
    assert resp.json()["data"]["has_token"] is True


async def test_sources_can_be_listed_and_filtered_by_project(client, project_id):
    await _attach(client, project_id)
    listing = (await client.get(f"/v1/sources?project_id={project_id}")).json()["data"]
    assert len(listing) == 1
    assert listing[0]["project_id"] == project_id


# ── Watching ─────────────────────────────────────────────────────────────────


async def test_watching_can_be_turned_on_and_the_interval_bounded(client, project_id):
    created = (await _attach(client, project_id)).json()["data"]
    resp = await client.patch(
        f"/v1/sources/{created['id']}",
        json={"watch_enabled": True, "watch_interval_seconds": 900, "apply_policy": "non_breaking"},
    )
    body = resp.json()["data"]
    assert body["watch"]["enabled"] is True
    assert body["watch"]["interval_seconds"] == 900
    assert body["watch"]["apply_policy"] == "non_breaking"

    too_fast = await client.patch(
        f"/v1/sources/{created['id']}", json={"watch_interval_seconds": 5}
    )
    assert too_fast.status_code == 422


async def test_an_unknown_apply_policy_is_refused(client, project_id):
    created = (await _attach(client, project_id)).json()["data"]
    resp = await client.patch(f"/v1/sources/{created['id']}", json={"apply_policy": "yolo"})
    assert resp.status_code == 400


# ── Checking and drift ───────────────────────────────────────────────────────


async def test_checking_a_changed_source_records_drift_without_applying_it(
    client, project_id, session
):
    created = (await _attach(client, project_id)).json()["data"]

    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        resp = await client.post(f"/v1/sources/{created['id']}/check")

    body = resp.json()["data"]
    assert body["status"] == "changed"
    assert body["breaking"] == 1
    assert body["applied"] is False
    assert body["drift_report_id"]

    drift = (await client.get(f"/v1/sources/{created['id']}/drift")).json()["data"]
    assert len(drift) == 1
    assert drift[0]["counts"]["BREAKING"] == 1
    assert drift[0]["status"] == "open"
    assert drift[0]["withheld_reason"]


async def test_a_drift_report_lists_what_changed(client, project_id):
    created = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        await client.post(f"/v1/sources/{created['id']}/check")

    drift = (await client.get(f"/v1/sources/{created['id']}/drift")).json()["data"][0]
    codes = {change["code"] for change in drift["changes"]}
    assert "operation_removed" in codes
    assert drift["changes"][0]["category"] == "BREAKING"


async def test_applying_a_drift_report_updates_the_project(client, project_id, session):
    from sutr.models.openapi_project import OpenAPIProject

    created = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        check = await client.post(f"/v1/sources/{created['id']}/check")
    report_id = check.json()["data"]["drift_report_id"]

    resp = await client.post(f"/v1/sources/drift/{report_id}/apply")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == STATUS_APPLIED

    session.expire_all()
    project = session.get(OpenAPIProject, __import__("uuid").UUID(project_id))
    assert "deletePet" not in project.ir_json


async def test_applying_the_same_report_twice_is_refused(client, project_id):
    created = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        check = await client.post(f"/v1/sources/{created['id']}/check")
    report_id = check.json()["data"]["drift_report_id"]

    assert (await client.post(f"/v1/sources/drift/{report_id}/apply")).status_code == 200
    second = await client.post(f"/v1/sources/drift/{report_id}/apply")
    assert second.status_code == 409


async def test_a_drift_report_can_be_dismissed(client, project_id, session):
    created = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        check = await client.post(f"/v1/sources/{created['id']}/check")
    report_id = check.json()["data"]["drift_report_id"]

    resp = await client.post(f"/v1/sources/drift/{report_id}/dismiss")
    assert resp.json()["data"]["status"] == STATUS_DISMISSED
    assert resp.json()["data"]["resolved_at"]


async def test_applying_and_dismissing_are_audited(client, project_id, session):
    from sutr.models.audit_event import AuditEvent

    created = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        check = await client.post(f"/v1/sources/{created['id']}/check")
    await client.post(f"/v1/sources/drift/{check.json()['data']['drift_report_id']}/apply")

    actions = {row.action for row in session.exec(select(AuditEvent)).all()}
    assert "source.connected" in actions
    assert "source.drift_applied" in actions


async def test_drift_is_cursor_paginated(client, project_id):
    created = (await _attach(client, project_id)).json()["data"]
    # Alternate between two shapes so each check is a change.
    for index in range(4):
        connect, fetch = _url_connector(_spec_text(remove_delete=index % 2 == 0))
        with connect, fetch:
            await client.post(f"/v1/sources/{created['id']}/check")

    first = (await client.get(f"/v1/sources/{created['id']}/drift?limit=2")).json()
    assert len(first["data"]) == 2
    cursor = first["meta"]["next_cursor"]
    assert cursor
    second = (await client.get(f"/v1/sources/{created['id']}/drift?limit=2&cursor={cursor}")).json()
    assert {row["id"] for row in first["data"]} & {row["id"] for row in second["data"]} == set()


# ── Comparing sources ────────────────────────────────────────────────────────


async def test_two_sources_can_be_compared(client, project_id):
    primary = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        other = await client.post(
            "/v1/sources",
            json={
                "project_id": project_id,
                "connector": "url",
                "config": {"url": "https://gateway.example.com/openapi.json"},
                "role": "comparison",
                "label": "Gateway",
            },
        )
    assert other.status_code == 201
    other_id = other.json()["data"]["id"]

    resp = await client.post(f"/v1/sources/{primary['id']}/compare/{other_id}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["origin"] == "comparison"
    assert body["counts"]["BREAKING"] == 1
    assert "never applied" in body["withheld_reason"]


async def test_a_source_cannot_be_compared_with_itself(client, project_id):
    created = (await _attach(client, project_id)).json()["data"]
    resp = await client.post(f"/v1/sources/{created['id']}/compare/{created['id']}")
    assert resp.status_code == 400


# ── Removal ──────────────────────────────────────────────────────────────────


async def test_removing_a_source_takes_its_drift_reports_with_it(client, project_id, session):
    created = (await _attach(client, project_id)).json()["data"]
    connect, fetch = _url_connector(_spec_text(remove_delete=True))
    with connect, fetch:
        await client.post(f"/v1/sources/{created['id']}/check")
    assert session.exec(select(DriftReport)).all()

    resp = await client.delete(f"/v1/sources/{created['id']}")
    assert resp.status_code == 204
    session.expire_all()
    assert session.exec(select(ApiSource)).all() == []
    assert session.exec(select(DriftReport)).all() == []


async def test_one_tenants_source_is_invisible_to_another(client, session, test_org, project_id):
    import uuid

    from sutr.models.org import Org

    other_org = Org(id=uuid.uuid4(), name="Other")
    session.add(other_org)
    session.commit()

    created = (await _attach(client, project_id)).json()["data"]
    source = session.get(ApiSource, uuid.UUID(created["id"]))
    source.org_id = other_org.id
    session.add(source)
    session.commit()

    assert (await client.get(f"/v1/sources/{created['id']}")).status_code == 404
    assert (await client.get("/v1/sources")).json()["data"] == []
