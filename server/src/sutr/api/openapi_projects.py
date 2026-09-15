"""OpenAPI project endpoints: import a spec, inspect it, compile it to tools.

Compilation materializes a CustomApiIntegration row, so the generated tools
flow through the exact same install/discovery/approval/execution pipeline as
hand-built custom APIs — no parallel execution path.
"""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from sutr import events
from sutr.analytics import posthog_client
from sutr.api.custom_api import (
    _allocate_integration_id,
    _invalidate_tool_cache,
    _tools_to_json,
    _validate_base_url,
)
from sutr.authz import ensure_agent_can
from sutr.connections.errors import ConnectError
from sutr.connections.store import access_token, find_connection
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.events import topics
from sutr.mcp.notifications import notify_tools_changed
from sutr.mcp.refresh import refresh_one
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration import InstalledIntegration
from sutr.models.openapi_project import OpenAPIProject
from sutr.openapi import (
    ApiDefinition,
    CompileFilters,
    OpenAPIError,
    compile_definition,
    fetch_spec_from_url,
    fingerprint,
    normalize,
    parse_spec_text,
    substitute_server_url,
    translate_security,
)
from sutr.openapi.packaging import build_server_package
from sutr.openapi.sources import (
    detect_source_kind,
    discover_github_specs,
    fetch_from_github,
    fetch_from_swaggerhub,
    parse_github_url,
)
from sutr.services.audit import actor_from_agent_auth, record_audit
from sutr.token_auth import validate_token_auth_config

router = APIRouter(prefix="/api/openapi", tags=["openapi"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http_error(exc: OpenAPIError) -> HTTPException:
    """Render a compile failure, with every diagnostic when we have them.

    `message` names the first problem so a CLI line still reads well;
    `findings` carries all of them so a user can fix a specification in one
    pass rather than one error per round-trip (build prompt §16).
    """
    detail: dict = {"error": exc.code, "message": exc.message}
    if exc.findings:
        detail["findings"] = [f.model_dump(mode="json") for f in exc.findings]
        detail["finding_counts"] = _finding_counts(exc.findings)
    return HTTPException(status_code=400, detail=detail)


def _finding_counts(findings) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding.severity.value.lower()] += 1
    return counts


class ImportRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    # paste | upload | url | github | swaggerhub
    source_kind: str = "paste"
    content: str | None = None
    url: str | None = None
    workspace_id: uuid.UUID | None = None
    # upload: the original filename, kept as provenance so a project imported
    # from a file is still traceable to one.
    filename: str | None = Field(default=None, max_length=255)
    # github: pick one file when a repository holds several. Omit to
    # auto-discover the conventional spec file.
    path: str | None = Field(default=None, max_length=500)
    # Use the caller's stored GitHub connection instead of a pasted token.
    # An explicit `github_token` always wins, so a one-off token still works
    # for a user who happens to have connected an account.
    use_connection: bool = False
    # Credentials for private sources. Used for this request only and never
    # stored: re-supply them if you re-import.
    github_token: str | None = Field(default=None, max_length=500)
    swaggerhub_api_key: str | None = Field(default=None, max_length=500)
    # Ask SwaggerHub to inline external $refs before sending us the document.
    resolved: bool = True


class DiscoverRequest(BaseModel):
    """Look inside a GitHub repository before importing anything."""

    url: str = Field(min_length=1, max_length=2000)
    use_connection: bool = False
    github_token: str | None = Field(default=None, max_length=500)


async def _resolve_github_token(
    session: Session,
    agent_auth: AgentAuth,
    *,
    explicit: str | None,
    use_connection: bool,
) -> str | None:
    """Which GitHub credential this request travels with, if any.

    Precedence is deliberate: a pasted token beats a stored connection, and
    neither is invented. Anonymous GitHub access still works for public
    repositories, which is why "no credential" is a valid answer rather than
    an error.
    """
    if explicit:
        return explicit
    if not use_connection:
        return None
    if agent_auth.user is None:
        raise HTTPException(
            status_code=403,
            detail="A connected GitHub account belongs to a user; an API key must "
            "pass github_token instead.",
        )
    connection = find_connection(
        session, org_id=agent_auth.org.id, user_id=agent_auth.user.id, provider="github"
    )
    if connection is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_connected",
                "message": "No GitHub account is connected. Connect one, or paste a token.",
            },
        )
    try:
        return await access_token(session, connection)
    except ConnectError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})


SOURCE_KINDS = ("paste", "upload", "url", "github", "swaggerhub")


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


def resolve_compilation(project: OpenAPIProject, body: "CompileRequest"):
    """Shared front half of compile, package and generate: base URL, tools, auth.

    Returns (definition, base_url, compile_result, auth, warnings). Raises
    HTTPException on invalid input.
    """
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
    return definition, base_url, result, auth, warnings


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
                "lint_findings": [f.model_dump(mode="json") for f in definition.lint_findings],
                "lint_summary": _finding_counts(definition.lint_findings),
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

    if body.source_kind not in SOURCE_KINDS:
        raise HTTPException(
            status_code=400, detail=f"source_kind must be one of: {', '.join(SOURCE_KINDS)}"
        )

    source_kind = body.source_kind
    # A generic URL that is really a GitHub or SwaggerHub link goes through that
    # provider's adapter instead of a raw GET. Without this, pasting a repository
    # URL into the plain URL source fetches an HTML page and fails on a parse
    # error that says nothing about the actual mistake.
    if source_kind == "url" and body.url:
        detected = detect_source_kind(body.url)
        if detected in ("github", "swaggerhub"):
            source_kind = detected

    source_url = body.url if source_kind in ("url", "github", "swaggerhub") else None
    provenance: dict = {}
    github_token = (
        await _resolve_github_token(
            session,
            agent_auth,
            explicit=body.github_token,
            use_connection=body.use_connection,
        )
        if source_kind == "github"
        else None
    )

    try:
        if source_kind == "github":
            if not body.url:
                raise HTTPException(
                    status_code=400,
                    detail="url is required — paste a GitHub repository or file URL",
                )
            fetched = await fetch_from_github(body.url, path=body.path, token=github_token)
            spec_text, source_url, provenance = (
                fetched.content,
                fetched.source_url,
                fetched.provenance,
            )
        elif source_kind == "swaggerhub":
            if not body.url:
                raise HTTPException(
                    status_code=400, detail="url is required — paste the SwaggerHub API URL"
                )
            fetched = await fetch_from_swaggerhub(
                body.url, api_key=body.swaggerhub_api_key, resolved=body.resolved
            )
            spec_text, source_url, provenance = (
                fetched.content,
                fetched.source_url,
                fetched.provenance,
            )
        elif source_kind == "url":
            if not body.url:
                raise HTTPException(status_code=400, detail="url is required for URL import")
            spec_text = await fetch_spec_from_url(body.url)
        else:
            if not body.content:
                raise HTTPException(status_code=400, detail="content is required")
            spec_text = body.content
            if source_kind == "upload" and body.filename:
                provenance = {"filename": body.filename}

        document = parse_spec_text(spec_text)
        definition = normalize(document)
    except OpenAPIError as exc:
        raise _http_error(exc)

    project = OpenAPIProject(
        org_id=agent_auth.org.id,
        workspace_id=body.workspace_id,
        name=body.name or definition.title[:80],
        source_kind=source_kind,
        source_url=source_url,
        spec_text=spec_text,
        ir_json=definition.model_dump_json(),
        warnings_json=json.dumps([w.model_dump() for w in definition.warnings]),
        # IR identity, so drift detection has a "before" without re-deriving it
        # and an IR from an older compiler is recognisable (build prompt §14).
        ir_version=definition.ir_version,
        ir_hash=fingerprint.fingerprint(definition),
        content_hash=fingerprint.content_hash(spec_text),
    )
    session.add(project)
    session.flush()

    # The control-plane pipeline's first two facts (LLD §3.1). Written in the
    # same transaction as the project, so an import that rolls back announces
    # nothing.
    events.publish(
        session,
        topics.API_UPLOADED,
        tenant_id=agent_auth.org.id,
        resource_id=str(project.id),
        producer="source-connectors",
        payload={
            "project_id": str(project.id),
            "source_kind": source_kind,
            "source_url": source_url,
            "api_title": definition.title,
            "api_version": definition.version,
        },
    )
    events.publish(
        session,
        topics.TRANSLATION_COMPLETED,
        tenant_id=agent_auth.org.id,
        resource_id=str(project.id),
        producer="translation",
        payload={
            "project_id": str(project.id),
            "openapi_version": definition.openapi_version,
            "source_dialect": definition.source_dialect,
            "operation_count": len(definition.operations),
            "warning_count": len(definition.warnings),
            "ir_version": definition.ir_version,
            "ir_hash": project.ir_hash,
        },
    )
    session.commit()
    session.refresh(project)

    posthog_client.capture(
        distinct_id=str(agent_auth.org.id),
        event="openapi_imported",
        properties={
            "source_kind": source_kind,
            "operation_count": len(definition.operations),
        },
    )
    detail = _serialize_project(project, detail=True)
    detail["provenance"] = provenance
    return detail


@router.post("/discover")
async def discover_specs(
    body: DiscoverRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """List the OpenAPI/Swagger files in a GitHub repository.

    Lets the wizard show a chooser when a repository holds several specs,
    instead of silently picking one. Nothing is stored.
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    token = await _resolve_github_token(
        session, agent_auth, explicit=body.github_token, use_connection=body.use_connection
    )
    try:
        kind = detect_source_kind(body.url)
        if kind != "github":
            raise OpenAPIError(
                "unsupported_source",
                "Discovery works on GitHub repositories. For SwaggerHub or a "
                "direct URL, import the specification straight away.",
            )
        target = parse_github_url(body.url)
        candidates, branch = await discover_github_specs(target, token)
    except OpenAPIError as exc:
        raise _http_error(exc)

    return {
        "source_kind": "github",
        "owner": target.owner,
        "repo": target.repo,
        "branch": branch,
        "candidates": [candidate.to_dict() for candidate in candidates],
    }


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

    definition, base_url, result, auth, warnings = resolve_compilation(project, body)
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
            auth_json=auth.model_dump_json(),
        )
        session.add(integration)
        session.flush()
    else:
        integration.name = integration_name
        integration.base_url = base_url
        integration.token_header = auth.token_header
        integration.token_format = auth.token_format
        integration.tools_json = tools_json
        # Re-record the translated schemes: a re-import may have changed which
        # credentials the API asks for.
        integration.auth_json = auth.model_dump_json()
        integration.updated_at = datetime.utcnow()
        session.add(integration)

    project.integration_db_id = integration.id
    project.status = "compiled"
    project.filters_json = body.filters.model_dump_json()
    project.server_url = body.server_url
    project.server_variables_json = json.dumps(body.server_variables)
    project.updated_at = _utcnow()
    session.add(project)

    events.publish(
        session,
        topics.MCP_GENERATED,
        tenant_id=agent_auth.org.id,
        resource_id=integration.integration_id,
        producer="mcp-generator",
        payload={
            "project_id": str(project.id),
            "integration_id": integration.integration_id,
            "tool_count": len(tools),
            "base_url": base_url,
        },
    )
    events.publish(
        session,
        topics.TOOL_REGISTERED,
        tenant_id=agent_auth.org.id,
        resource_id=integration.integration_id,
        producer="registry",
        payload={
            "integration_id": integration.integration_id,
            "name": integration_name,
            "tool_count": len(tools),
        },
    )

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


@router.post("/{project_id}/package")
def package_project(
    project_id: uuid.UUID,
    body: CompileRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> Response:
    """Generate a standalone MCP server package (zip) for this project.

    Accepts the same body as /compile (filters, server, auth, name);
    `dry_run` is ignored. Nothing is created server-side beyond an audit
    event — the package runs entirely on the user's own infrastructure, so
    the base URL is deliberately NOT SSRF-screened here (a private-network
    API is a legitimate target for a self-hosted server).
    """
    ensure_agent_can(session, agent_auth, "integrations:manage")
    project = session.get(OpenAPIProject, project_id)
    if project is None or project.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="OpenAPI project not found")

    definition, base_url, result, auth, _warnings = resolve_compilation(project, body)

    try:
        filename, data = build_server_package(
            name=body.integration_name or project.name,
            base_url=base_url,
            token_header=auth.token_header,
            token_format=auth.token_format,
            tools=[ct.tool for ct in result.tools],
            api_title=definition.title,
            api_version=definition.version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="openapi.package_generated",
        summary=f"Standalone MCP server package generated for '{project.name}'",
        target_type="openapi_project",
        target_id=str(project.id),
        metadata={"tool_count": len(result.tools), "filename": filename},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()

    posthog_client.capture(
        distinct_id=str(agent_auth.org.id),
        event="openapi_package_generated",
        properties={"tool_count": len(result.tools)},
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
