"""OpenAPI project API: import → inspect → compile → execute end to end."""

import json
import uuid
from unittest.mock import AsyncMock, patch

from sqlmodel import select

from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration import InstalledIntegration
from sutr.models.log import LogEntry
from sutr.models.openapi_project import OpenAPIProject
from sutr.models.org_membership import OrgMembership
from sutr.models.tool_execution import ToolExecutionSetting

PETSTORE = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.2.0", "description": "Pets over HTTP."},
    "servers": [{"url": "https://api.petstore.example.com/v1"}],
    "components": {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
    },
    "security": [{"key": []}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets",
                "tags": ["pets"],
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "tags": ["pets"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "tag": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/admin/purge": {
            "post": {
                "operationId": "purgeEverything",
                "tags": ["admin"],
                "summary": "Danger",
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
}


def _safe_url_stub(url, **kwargs):
    return None


async def _import(client, spec=None, **overrides) -> dict:
    body = {"source_kind": "paste", "content": json.dumps(spec or PETSTORE), **overrides}
    resp = await client.post("/api/openapi/import", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_import_paste_and_inspect(client):
    data = await _import(client)
    assert data["api_title"] == "Petstore"
    assert data["api_version"] == "1.2.0"
    assert data["operation_count"] == 3
    assert data["suggested_auth"]["token_header"] == "X-Api-Key"
    assert sorted(data["tags"]) == ["admin", "pets"]
    ops = {o["operation_id"] for o in data["operations"]}
    assert ops == {"listPets", "createPet", "purgeEverything"}

    listing = await client.get("/api/openapi")
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    detail = await client.get(f"/api/openapi/{data['id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "imported"


async def test_import_rejects_invalid_spec(client):
    resp = await client.post(
        "/api/openapi/import", json={"source_kind": "paste", "content": "not: an: openapi: doc"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] in ("parse_error", "missing_version", "not_an_object")

    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps({"swagger": "2.0", "paths": {}})},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_version"


async def test_import_from_url_uses_fetcher(client, monkeypatch):
    fetched = AsyncMock(return_value=json.dumps(PETSTORE))
    monkeypatch.setattr("sutr.api.openapi_projects.fetch_spec_from_url", fetched)
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "url", "url": "https://specs.example.com/petstore.json"},
    )
    assert resp.status_code == 201
    assert resp.json()["source_url"] == "https://specs.example.com/petstore.json"
    fetched.assert_awaited_once()


async def test_url_import_routes_github_links_through_the_adapter(client, monkeypatch):
    """A repository URL pasted into the generic `url` source must not be GET'd.

    A raw fetch would return the HTML repository page and fail on a parse error
    that says nothing about the real mistake, so the provider is detected and its
    adapter used instead.
    """
    raw = AsyncMock(return_value=json.dumps(PETSTORE))
    monkeypatch.setattr("sutr.api.openapi_projects.fetch_spec_from_url", raw)

    from sutr.openapi.sources import FetchedSpec

    adapter = AsyncMock(
        return_value=FetchedSpec(
            content=json.dumps(PETSTORE),
            source_kind="github",
            source_url="https://github.com/o/r/blob/main/openapi.yaml",
            provenance={"owner": "o", "repo": "r", "branch": "main", "path": "openapi.yaml"},
        )
    )
    monkeypatch.setattr("sutr.api.openapi_projects.fetch_from_github", adapter)

    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "url", "url": "https://github.com/o/r"},
    )
    assert resp.status_code == 201
    body = resp.json()
    # The stored kind reflects what actually happened, not what was requested.
    assert body["source_kind"] == "github"
    assert body["provenance"]["repo"] == "r"
    adapter.assert_awaited_once()
    raw.assert_not_awaited()


async def test_compile_dry_run_previews_without_side_effects(client, session, test_org):
    project = await _import(client)
    resp = await client.post(
        f"/api/openapi/{project['id']}/compile",
        json={"dry_run": True, "filters": {"exclude_tags": ["admin"]}},
    )
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["tools"]}
    assert names == {"list_pets", "create_pet"}
    assert body["auth"]["token_header"] == "X-Api-Key"
    assert body["base_url"] == "https://api.petstore.example.com/v1"

    assert session.exec(select(CustomApiIntegration)).first() is None
    session.expire_all()
    stored = session.get(OpenAPIProject, uuid.UUID(project["id"]))
    assert stored.status == "imported"


async def test_compile_creates_integration_and_recompile_updates(
    client, session, test_org, monkeypatch
):
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    project = await _import(client)

    resp = await client.post(
        f"/api/openapi/{project['id']}/compile",
        json={"filters": {"exclude_tags": ["admin"]}, "integration_name": "Petstore Tools"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    integration_id = body["integration_id"]
    assert body["project"]["status"] == "compiled"

    session.expire_all()
    integration = session.exec(select(CustomApiIntegration)).one()
    assert integration.name == "Petstore Tools"
    assert integration.token_header == "X-Api-Key"
    tools = json.loads(integration.tools_json)
    assert {t["name"] for t in tools} == {"list_pets", "create_pet"}

    # The compiled integration shows up in the catalog like any custom API.
    catalog = await client.get(f"/api/integrations/{integration_id}")
    assert catalog.status_code == 200
    assert catalog.json()["type"] == "custom"

    # Recompiling (e.g. after changing filters) updates the SAME integration.
    resp = await client.post(
        f"/api/openapi/{project['id']}/compile",
        json={"filters": {}, "integration_name": "Petstore Tools v2"},
    )
    assert resp.status_code == 200
    assert resp.json()["integration_id"] == integration_id
    session.expire_all()
    integrations = session.exec(select(CustomApiIntegration)).all()
    assert len(integrations) == 1
    assert {t["name"] for t in json.loads(integrations[0].tools_json)} == {
        "list_pets",
        "create_pet",
        "purge_everything",
    }


async def test_compiled_tool_executes_through_pipeline(client, session, test_org, monkeypatch):
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    project = await _import(client)
    resp = await client.post(f"/api/openapi/{project['id']}/compile", json={})
    integration_id = resp.json()["integration_id"]

    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id=integration_id,
            type="custom",
            url="https://api.petstore.example.com/v1",
            auth_method="token",
            connected=True,
        )
    )
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id,
            integration_id=integration_id,
            tool_name="list_pets",
            mode="allow",
        )
    )
    session.commit()

    mock_result = {"content": [{"type": "text", "text": '[{"name": "Rex"}]'}], "isError": False}
    with patch(
        "sutr.api_client.call_tool", new_callable=AsyncMock, return_value=mock_result
    ) as mocked:
        resp = await client.post(
            f"/api/tools/{integration_id}/call",
            json={"tool_name": "list_pets", "args": {"limit": 5}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == mock_result

    # The compiled ApiTool definition was resolved and handed to the executor.
    tool_def = mocked.call_args.args[1]
    assert tool_def.name == "list_pets"
    assert tool_def.method == "GET"
    assert tool_def.path == "/pets"

    session.expire_all()
    log = session.exec(
        select(LogEntry).where(LogEntry.org_id == test_org.id).where(LogEntry.outcome == "executed")
    ).one()
    assert log.integration_id == integration_id
    assert log.tool_name == "list_pets"


async def test_viewer_cannot_import_or_compile(client, session, test_user, test_org):
    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = "viewer"
    session.add(membership)
    session.commit()

    resp = await client.post(
        "/api/openapi/import", json={"source_kind": "paste", "content": json.dumps(PETSTORE)}
    )
    assert resp.status_code == 403


async def test_delete_project_keeps_generated_integration(client, session, test_org, monkeypatch):
    monkeypatch.setattr("sutr.api.custom_api.validate_safe_url", _safe_url_stub)
    project = await _import(client)
    resp = await client.post(f"/api/openapi/{project['id']}/compile", json={})
    assert resp.status_code == 200

    resp = await client.delete(f"/api/openapi/{project['id']}")
    assert resp.status_code == 204

    session.expire_all()
    assert session.exec(select(OpenAPIProject)).first() is None
    assert session.exec(select(CustomApiIntegration)).one() is not None
