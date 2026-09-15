"""MCP generation: turn a compiled API into a validated, signed runtime artifact.

The LLD §3.6 interface is `POST {ir_uri, knowledge_uri, runtime:"python"}`, and
that is what `create_runtime` accepts. `ir_uri` names an OpenAPI project —
Sutr's IR lives in a project row rather than at a URL, so the accepted forms
are `sutr://openapi-projects/{id}` and a bare project id, and anything else is
refused rather than fetched. Nothing here dereferences an external URL: an
endpoint that fetched an arbitrary `ir_uri` would be a request-forgery
primitive wearing a specification's clothes.

`knowledge_uri` is optional and names the project whose documentation should be
folded into the build. It defaults to the same project, which is the case that
actually happens; pointing it elsewhere is for a provider who documented one
API and generated another from it.

Everything is scoped to the caller's org, in the query, on every route. An
artifact belonging to another tenant reads as 404, never 403 — a 403 would
confirm the artifact exists.
"""

import json
import re
import uuid

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlmodel import Session

from sutr.api.openapi_projects import CompileRequest, resolve_compilation
from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import InvalidRequestError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.generation import artifacts, pipeline, signing, validation
from sutr.models.openapi_project import OpenAPIProject
from sutr.models.runtime_artifact import RUNTIME_PYTHON
from sutr.openapi import packaging
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/generation", tags=["generation"])

# Generating a runtime is compiling a provider's API, and the artifact carries
# the whole compiled tool set. The role that may compile is the role that may
# read the build; there is no lower read tier here.
MANAGE = "integrations:manage"

_PROJECT_URI = re.compile(
    r"^(?:sutr://(?:openapi-projects|projects|documentation/projects)/)?"
    r"(?P<id>[0-9a-fA-F-]{36})$"
)


class CreateRuntimeRequest(BaseModel):
    """The LLD §3.6 generation interface, plus the compile inputs Sutr needs."""

    ir_uri: str | None = None
    # Accepted as an alias for ir_uri, because everything else in this API
    # takes ids and requiring a URI form for one field would be a trap.
    project_id: uuid.UUID | None = None
    knowledge_uri: str | None = None
    runtime: str = RUNTIME_PYTHON
    # The same compile inputs `POST /api/openapi/{id}/package` takes: which
    # operations, which server, which credential header.
    compile: CompileRequest = CompileRequest()


def _project_id(value: str | None, field: str) -> uuid.UUID:
    match = _PROJECT_URI.match((value or "").strip())
    if not match:
        raise InvalidRequestError(
            f"{field} must be a project id or sutr://openapi-projects/{{id}}; "
            "external URIs are not fetched."
        )
    return uuid.UUID(match.group("id"))


def _load_project(session: Session, project_id: uuid.UUID, org_id: uuid.UUID) -> OpenAPIProject:
    project = session.get(OpenAPIProject, project_id)
    if project is None or project.org_id != org_id:
        raise NotFoundError(f"OpenAPI project {project_id} was not found.")
    return project


def _load_artifact(session: Session, artifact_id: uuid.UUID, org_id: uuid.UUID):
    artifact = artifacts.get(session, artifact_id, org_id)
    if artifact is None:
        raise NotFoundError(f"Runtime artifact {artifact_id} was not found.")
    return artifact


@router.get("/capabilities")
def capabilities(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What this install can generate, check and sign.

    Reported rather than assumed, for the same reason the documentation service
    reports its parsers: whether an artifact was scanned, tested and signed
    depends on what the operator configured, and a caller should be able to
    find that out before trusting a `validated` status.
    """
    ensure_agent_can(session, agent_auth, MANAGE)
    return envelope(
        {
            "runtimes": [
                {
                    "id": runtime,
                    "available": runtime in pipeline.SUPPORTED_RUNTIMES,
                    "unavailable_reason": pipeline.runtime_support(runtime)[1],
                }
                for runtime in pipeline.DECLARED_RUNTIMES
            ],
            "template_version": packaging.TEMPLATE_VERSION,
            "artifact_version": artifacts.ARTIFACT_VERSION,
            "validation": validation.describe(),
            "signing": signing.describe(),
        }
    )


@router.post("/runtimes", status_code=201)
async def create_runtime(
    body: CreateRuntimeRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Generate, validate, sign and store one runtime artifact."""
    ensure_agent_can(session, agent_auth, MANAGE)
    org_id = agent_auth.org.id

    if body.project_id is not None:
        project_id = body.project_id
    else:
        project_id = _project_id(body.ir_uri, "ir_uri")
    project = _load_project(session, project_id, org_id)
    knowledge_project_id = (
        _project_id(body.knowledge_uri, "knowledge_uri") if body.knowledge_uri else project_id
    )
    if knowledge_project_id != project_id:
        # Validated so a knowledge_uri cannot be used to probe for the
        # existence of another tenant's project.
        _load_project(session, knowledge_project_id, org_id)

    definition, base_url, result, auth, _warnings = resolve_compilation(project, body.compile)

    try:
        outcome = await pipeline.generate(
            session,
            org_id=org_id,
            project_id=project_id,
            name=body.compile.integration_name or project.name,
            base_url=base_url,
            token_header=auth.token_header,
            token_format=auth.token_format,
            tools=[compiled.tool for compiled in result.tools],
            api_title=definition.title,
            api_version=definition.version,
            ir_version=project.ir_version,
            ir_hash=project.ir_hash or "",
            runtime=body.runtime,
            created_by_user_id=agent_auth.user.id if agent_auth.user else None,
        )
    except pipeline.GenerationError as exc:
        # The failure event was already published inside the pipeline where it
        # happened; committing here is what lets it out of the outbox.
        session.commit()
        raise InvalidRequestError(exc.message, code=exc.code)

    record_audit(
        session,
        org_id=org_id,
        action="generation.runtime_generated",
        summary=(
            f"Runtime artifact {'reused' if outcome.reused else 'generated'} for "
            f"'{project.name}' ({outcome.artifact.status})"
        ),
        target_type="runtime_artifact",
        target_id=str(outcome.artifact.id),
        metadata={
            "build_hash": outcome.artifact.build_hash,
            "status": outcome.artifact.status,
            "tool_count": outcome.artifact.tool_count,
            "reused": outcome.reused,
        },
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(outcome.artifact)
    return envelope(outcome.as_dict())


@router.get("/runtimes")
def list_runtimes(
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    rows = artifacts.list_for_org(
        session, org_id=agent_auth.org.id, project_id=project_id, limit=limit, offset=offset
    )
    return envelope({"artifacts": [artifacts.serialize(row) for row in rows]})


@router.get("/runtimes/{artifact_id}")
def get_runtime(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    artifact = _load_artifact(session, artifact_id, agent_auth.org.id)
    return envelope(artifacts.serialize(artifact, detail=True))


@router.get("/runtimes/{artifact_id}/manifest")
def get_manifest(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    artifact = _load_artifact(session, artifact_id, agent_auth.org.id)
    return envelope(json.loads(artifact.manifest_json or "{}"))


@router.get("/runtimes/{artifact_id}/sbom")
def get_sbom(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> Response:
    """The CycloneDX document, served as CycloneDX rather than wrapped.

    An SBOM is fed to tools that expect the document itself; putting it inside
    a response envelope would mean every consumer had to unwrap it first.
    """
    ensure_agent_can(session, agent_auth, MANAGE)
    artifact = _load_artifact(session, artifact_id, agent_auth.org.id)
    filename = f"{artifact.slug}-{artifact.build_hash[:12]}.cdx.json"
    return Response(
        content=artifact.sbom_json,
        media_type="application/vnd.cyclonedx+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runtimes/{artifact_id}/validation")
def get_validation(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, MANAGE)
    artifact = _load_artifact(session, artifact_id, agent_auth.org.id)
    return envelope(json.loads(artifact.validation_json or "{}"))


@router.get("/runtimes/{artifact_id}/package")
def download_package(
    artifact_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> Response:
    """The exact bytes that were validated, with their digest in a header."""
    ensure_agent_can(session, agent_auth, MANAGE)
    artifact = _load_artifact(session, artifact_id, agent_auth.org.id)
    return Response(
        content=artifact.package_zip,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.slug}-mcp-server.zip"',
            "X-Sutr-Package-Sha256": artifact.package_sha256,
            "X-Sutr-Build-Hash": artifact.build_hash,
        },
    )
