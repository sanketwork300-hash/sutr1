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

    # Swagger 2.0 is converted rather than refused (build prompt §17), so a
    # 2.0 document with no `info` now fails on what is actually wrong with it.
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps({"swagger": "2.0", "paths": {}})},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_spec"


async def test_import_accepts_and_converts_a_swagger_2_document(client):
    swagger2 = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "1.0.0"},
        "host": "legacy.example.com",
        "basePath": "/api",
        "schemes": ["https"],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List things.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    resp = await client.post(
        "/api/openapi/import", json={"source_kind": "paste", "content": json.dumps(swagger2)}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["api_title"] == "Legacy"
    assert body["openapi_version"].startswith("3.0")

    detail = await client.get(f"/api/openapi/{body['id']}")
    assert detail.json()["servers"][0]["url"] == "https://legacy.example.com/api"


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


# ── upload source and connected-account imports ──────────────────────────────


async def test_upload_records_the_filename_as_provenance(client):
    resp = await client.post(
        "/api/openapi/import",
        json={
            "source_kind": "upload",
            "content": json.dumps(PETSTORE),
            "filename": "petstore-v3.yaml",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source_kind"] == "upload"
    assert body["provenance"] == {"filename": "petstore-v3.yaml"}
    # An uploaded document has no URL to point back at.
    assert body["source_url"] is None


async def test_upload_without_content_is_rejected(client):
    resp = await client.post(
        "/api/openapi/import", json={"source_kind": "upload", "filename": "empty.yaml"}
    )
    assert resp.status_code == 400


async def test_import_with_use_connection_and_no_connection_says_what_to_do(client):
    resp = await client.post(
        "/api/openapi/import",
        json={
            "source_kind": "github",
            "url": "https://github.com/acme/api",
            "use_connection": True,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_connected"


async def test_discover_with_use_connection_and_no_connection_is_the_same_error(client):
    resp = await client.post(
        "/api/openapi/discover",
        json={"url": "https://github.com/acme/api", "use_connection": True},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_connected"


async def test_the_stored_connection_supplies_the_github_token(
    client, session, test_org, test_user, monkeypatch
):
    from sutr.connections.store import save_connection

    save_connection(
        session,
        org_id=test_org.id,
        user_id=test_user.id,
        provider="github",
        access_token_value="gho_from_connection",
        refresh_token_value=None,
        scopes="repo",
        account_label="octocat",
        expires=None,
        metadata={},
    )
    session.commit()

    seen = {}

    async def fake_fetch(url, *, path=None, token=None):
        seen["token"] = token
        from sutr.openapi.sources import FetchedSpec

        return FetchedSpec(
            content=json.dumps(PETSTORE),
            source_kind="github",
            source_url="https://github.com/acme/api/blob/main/openapi.json",
            provenance={"owner": "acme", "repo": "api"},
        )

    monkeypatch.setattr("sutr.api.openapi_projects.fetch_from_github", fake_fetch)
    resp = await client.post(
        "/api/openapi/import",
        json={
            "source_kind": "github",
            "url": "https://github.com/acme/api",
            "use_connection": True,
        },
    )
    assert resp.status_code == 201
    assert seen["token"] == "gho_from_connection"


async def test_an_explicit_token_beats_the_stored_connection(
    client, session, test_org, test_user, monkeypatch
):
    """A one-off token still works for someone who happens to have connected
    an account - otherwise the connection becomes impossible to bypass."""
    from sutr.connections.store import save_connection

    save_connection(
        session,
        org_id=test_org.id,
        user_id=test_user.id,
        provider="github",
        access_token_value="gho_from_connection",
        refresh_token_value=None,
        scopes="repo",
        account_label="octocat",
        expires=None,
        metadata={},
    )
    session.commit()

    seen = {}

    async def fake_fetch(url, *, path=None, token=None):
        seen["token"] = token
        from sutr.openapi.sources import FetchedSpec

        return FetchedSpec(
            content=json.dumps(PETSTORE),
            source_kind="github",
            source_url="https://github.com/acme/api",
            provenance={},
        )

    monkeypatch.setattr("sutr.api.openapi_projects.fetch_from_github", fake_fetch)
    resp = await client.post(
        "/api/openapi/import",
        json={
            "source_kind": "github",
            "url": "https://github.com/acme/api",
            "use_connection": True,
            "github_token": "ghp_explicit",
        },
    )
    assert resp.status_code == 201
    assert seen["token"] == "ghp_explicit"


# ── Linting (build prompt §16) ───────────────────────────────────────────────


async def test_lint_rules_endpoint_lists_every_rule(client):
    resp = await client.get("/api/openapi/lint/rules")
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) > 20
    for entry in rules:
        assert entry["rule_id"]
        assert entry["severity"] in ("ERROR", "WARNING", "INFO")
        assert entry["summary"]
        assert entry["documentation"].endswith(entry["rule_id"])


async def test_lint_endpoint_reports_findings_without_importing(client):
    resp = await client.post(
        "/api/openapi/lint",
        json={
            "content": json.dumps({"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {}}}})
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["error"] >= 3
    assert {f["rule_id"] for f in body["findings"]} >= {"oas3-schema"}
    # Nothing was stored.
    listing = await client.get("/api/openapi")
    assert listing.json() == []


async def test_lint_endpoint_rejects_a_document_with_no_version(client):
    resp = await client.post("/api/openapi/lint", json={"content": json.dumps({"paths": {}})})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "missing_version"


async def test_invalid_spec_import_returns_all_findings(client):
    resp = await client.post(
        "/api/openapi/import",
        json={
            "source_kind": "paste",
            "content": json.dumps({"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {}}}}),
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_spec"
    assert len(detail["findings"]) >= 3
    assert detail["finding_counts"]["error"] >= 3
    first = detail["findings"][0]
    assert set(first) == {
        "rule_id",
        "severity",
        "message",
        "location",
        "json_pointer",
        "documentation",
        "remediation",
    }


async def test_project_detail_carries_lint_findings(client):
    resp = await client.post(
        "/api/openapi/import", json={"source_kind": "paste", "content": json.dumps(PETSTORE)}
    )
    assert resp.status_code == 201
    detail = await client.get(f"/api/openapi/{resp.json()['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert "lint_findings" in body
    assert set(body["lint_summary"]) == {"error", "warning", "info"}
