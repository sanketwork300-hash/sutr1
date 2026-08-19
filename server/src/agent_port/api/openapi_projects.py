"""OpenAPI project endpoints: import a spec, inspect it, compile it to tools.

Compilation materializes a CustomApiIntegration row, so the generated tools
flow through the exact same install/discovery/approval/execution pipeline as
hand-built custom APIs — no parallel execution path.
"""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from agent_port.analytics import posthog_client
from agent_port.api.custom_api import (
    _allocate_integration_id,
    _invalidate_tool_cache,
    _tools_to_json,
    _validate_base_url,
)
from agent_port.authz import ensure_agent_can
from agent_port.db import get_session
from agent_port.dependencies import AgentAuth, get_agent_auth
from agent_port.mcp.notifications import notify_tools_changed
from agent_port.mcp.refresh import refresh_one
from agent_port.models.custom_api_integration import CustomApiIntegration
from agent_port.models.integration import InstalledIntegration
from agent_port.models.openapi_project import OpenAPIProject
from agent_port.openapi import (
    ApiDefinition,
    CompileFilters,
    OpenAPIError,
    compile_definition,
    fetch_spec_from_url,
    normalize,
    parse_spec_text,
    substitute_server_url,
    translate_security,
)
from agent_port.token_auth import validate_token_auth_config

router = APIRouter(prefix="/api/openapi", tags=["openapi"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http_error(exc: OpenAPIError) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})


class ImportRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    source_kind: str = "paste"  # paste | upload | url
    content: str | None = None
    url: str | None = None
    workspace_id: uuid.UUID | None = None


class AuthConfig(BaseModel):
    token_header: str = ""
    token_format: str = ""


class CompileRequest(BaseModel):
    filters: CompileFilters = CompileFilters()
    server_url: str | None = None
    server_variables: dict[str, str] = {}
    auth: AuthConfig | None = None  # None → use the spec's translated security
    integration_name: str | None = Field(default=None, max_length=80)
    dry_run: bool = False


def _definition(project: OpenAPIProject) -> ApiDefinition:
    return ApiDefinition.model_validate_json(project.ir_json)


def _operation_summary(definition: ApiDefinition) -> list[dict]:
    return [
        {
            "operation_id": op.operation_id,
            "method": op.method,
            "path": op.path,
            "summary": op.summary,
            "tags": op.tags,
            "deprecated": op.deprecated,
        }
        for op in definition.operations
    ]


def _serialize_project(project: OpenAPIProject, *, detail: bool = False) -> dict:
    definition = _definition(project)
    data = {
        "id": str(project.id),
        "name": project.name,
        "source_kind": project.source_kind,
        "source_url": project.source_url,
        "status": project.status,
        "api_title": definition.title,
        "api_version": definition.version,
        "openapi_version": definition.openapi_version,
        "operation_count": len(definition.operations),
        "integration_db_id": (
            str(project.integration_db_id) if project.integration_db_id else None
        ),
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "warnings": json.loads(project.warnings_json),
    }
    if detail:
        auth = translate_security(definition)
        data.update(
            {
                "description": definition.description,
                "servers": [s.model_dump() for s in definition.servers],
                "security_schemes": [s.model_dump() for s in definition.security_schemes],
                "suggested_auth": auth.model_dump(),
                "operations": _operation_summary(definition),
                "tags": sorted({t for op in definition.operations for t in op.tags}),
                "filters": json.loads(project.filters_json),
                "server_url": project.server_url,
                "server_variables": json.loads(project.server_variables_json),
            }
        )
    return data


@router.get("")
def list_projects(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    rows = session.exec(
        select(OpenAPIProject).where(OpenAPIProject.org_id == agent_auth.org.id)
    ).all()
    return [_serialize_project(p) for p in rows]


@router.post("/import", status_code=201)
async def import_spec(
    body: ImportRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "integrations:manage")

    if body.source_kind not in ("paste", "upload", "url"):
        raise HTTPException(status_code=400, detail="source_kind must be paste, upload, or url")

    try:
        if body.source_kind == "url":
            if not body.url:
                raise HTTPException(status_code=400, detail="url is required for URL import")
            spec_text = await fetch_spec_from_url(body.url)
        else:
            if not body.content:
                raise HTTPException(status_code=400, detail="content is required")
            spec_text = body.content

        document = parse_spec_text(spec_text)
        definition = normalize(document)
    except OpenAPIError as exc:
        raise _http_error(exc)

    project = OpenAPIProject(
        org_id=agent_auth.org.id,
        workspace_id=body.workspace_id,
        name=body.name or definition.title[:80],
        source_kind=body.source_kind,
        source_url=body.url if body.source_kind == "url" else None,
        spec_text=spec_text,
        ir_json=definition.model_dump_json(),
        warnings_json=json.dumps([w.model_dump() for w in definition.warnings]),
    )
    session.add(project)
    session.commit()
    session.refresh(project)

    posthog_client.capture(
        distinct_id=str(agent_auth.org.id),
        event="openapi_imported",
        properties={
            "source_kind": body.source_kind,
            "operation_count": len(definition.operations),
        },
    )
    return _serialize_project(project, detail=True)


@router.get("/{project_id}")
def get_project(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    project = session.get(OpenAPIProject, project_id)
    if project is None or project.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="OpenAPI project not found")
    return _serialize_project(project, detail=True)


@router.post("/{project_id}/compile")
def compile_project(
    project_id: uuid.UUID,
    body: CompileRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    project = session.get(OpenAPIProject, project_id)
    if project is None or project.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="OpenAPI project not found")

    definition = _definition(project)

    # Resolve the base URL: explicit choice, else the spec's first server.
    try:
        if body.server_url:
            base_url = body.server_url
            for server in definition.servers:
                if server.url == body.server_url:
                    base_url = substitute_server_url(server, body.server_variables)
                    break
        elif definition.servers:
            base_url = substitute_server_url(definition.servers[0], body.server_variables)
        else:
            raise OpenAPIError(
                "no_server",
                "The specification declares no servers — provide server_url explicitly.",
            )
        result = compile_definition(definition, body.filters)
    except OpenAPIError as exc:
        raise _http_error(exc)

    # Auth: explicit config wins; otherwise the translated spec security.
    translated = translate_security(definition)
    auth = body.auth or AuthConfig(
        token_header=translated.token_header, token_format=translated.token_format
    )
    if auth.token_header or auth.token_format:
        try:
            validate_token_auth_config(auth.token_header, auth.token_format)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    warnings = [w.model_dump() for w in result.warnings] + [
        w.model_dump() for w in translated.warnings
    ]
    tools = [ct.tool for ct in result.tools]
    preview = {
        "tools": [
            {
                "name": ct.tool.name,
                "description": ct.tool.description,
                "method": ct.method,
                "path": ct.path,
                "tags": ct.tags,
                "operation_id": ct.operation_id,
                "renamed_from": ct.renamed_from,
                "param_count": len(ct.tool.params),
            }
            for ct in result.tools
        ],
        "warnings": warnings,
        "auth": auth.model_dump(),
        "base_url": base_url,
        "dry_run": body.dry_run,
    }
    if body.dry_run:
        return preview

    _validate_base_url(base_url)

    integration_name = body.integration_name or project.name
    tools_json = _tools_to_json(tools)

    integration: CustomApiIntegration | None = None
    if project.integration_db_id:
        integration = session.get(CustomApiIntegration, project.integration_db_id)
    if integration is None:
        integration = CustomApiIntegration(
            org_id=agent_auth.org.id,
            integration_id=_allocate_integration_id(session, agent_auth.org.id, integration_name),
            name=integration_name,
            description=definition.description or f"Imported from OpenAPI: {definition.title}",
            base_url=base_url,
            token_header=auth.token_header,
            token_format=auth.token_format,
            tools_json=tools_json,
        )
        session.add(integration)
        session.flush()
    else:
        integration.name = integration_name
        integration.base_url = base_url
        integration.token_header = auth.token_header
        integration.token_format = auth.token_format
        integration.tools_json = tools_json
        integration.updated_at = datetime.utcnow()
        session.add(integration)

    project.integration_db_id = integration.id
    project.status = "compiled"
    project.filters_json = body.filters.model_dump_json()
    project.server_url = body.server_url
    project.server_variables_json = json.dumps(body.server_variables)
    project.updated_at = _utcnow()
    session.add(project)

    # Refresh downstream consumers exactly like a custom-API edit does.
    _invalidate_tool_cache(session, agent_auth.org.id, integration.integration_id)
    session.commit()
    session.refresh(integration)

    installed = session.exec(
        select(InstalledIntegration)
        .where(InstalledIntegration.org_id == agent_auth.org.id)
        .where(InstalledIntegration.integration_id == integration.integration_id)
    ).first()
    if installed:
        background_tasks.add_task(refresh_one, agent_auth.org.id, integration.integration_id)
    else:
        background_tasks.add_task(notify_tools_changed, agent_auth.org.id)

    posthog_client.capture(
        distinct_id=str(agent_auth.org.id),
        event="openapi_compiled",
        properties={"tool_count": len(tools)},
    )

    return {
        **preview,
        "integration_db_id": str(integration.id),
        "integration_id": integration.integration_id,
        "project": _serialize_project(project),
    }


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    project = session.get(OpenAPIProject, project_id)
    if project is None or project.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="OpenAPI project not found")
    # The generated integration (if any) lives on independently and is managed
    # through the custom-API endpoints; only the import artifact is removed.
    session.delete(project)
    session.commit()
