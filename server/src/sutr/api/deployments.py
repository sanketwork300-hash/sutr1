"""Deployment endpoints: run generated MCP servers on a provider.

Create snapshots the generated package onto the row, stores the runtime token
through the secrets backend (never in the package or the image), and builds
in a background task. Reads reconcile stored status with the provider.
"""

import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from sutr.api.openapi_projects import CompileRequest, _resolve_compilation
from sutr.authz import ensure_agent_can
from sutr.connections.store import load_connection
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.deploy.base import ProviderError, ProviderTarget
from sutr.deploy.credentials import deployment_config, resolve_target
from sutr.deploy.registry import get_provider, list_providers, provider_enabled
from sutr.models.deployment import Deployment
from sutr.models.openapi_project import OpenAPIProject
from sutr.openapi.packaging import build_server_package, env_var_for, slugify
from sutr.secrets.records import delete_secret, upsert_secret
from sutr.services.audit import actor_from_agent_auth, record_audit, request_meta
from sutr.services.deployments import refresh_status, run_deployment

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


class CreateDeploymentRequest(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=80)
    provider: str = "docker"
    # Which connected account authorizes a cloud provider. Required for every
    # provider that declares one; ignored by the local Docker provider.
    connection_id: uuid.UUID | None = None
    # Provider placement: project/subscription/account, region, registry, and
    # the IAM role ARNs AWS needs. Keys come from the provider's own
    # `config_fields`, so adding a provider needs no change here.
    provider_config: dict[str, str] = {}
    # Runtime API token for the upstream, stored via the secrets backend and
    # injected as an env var at run time. None → deploy without credentials.
    token: str | None = None
    compile: CompileRequest = CompileRequest()


def _serialize(deployment: Deployment) -> dict:
    state = json.loads(deployment.provider_state_json)
    return {
        "id": str(deployment.id),
        "name": deployment.name,
        "slug": deployment.slug,
        "provider": deployment.provider,
        "status": deployment.status,
        "url": deployment.url,
        "health_url": state.get("health_url"),
        "error": deployment.error,
        "tool_count": deployment.tool_count,
        "project_id": str(deployment.project_id) if deployment.project_id else None,
        "connection_id": str(deployment.connection_id) if deployment.connection_id else None,
        "config": deployment_config(deployment),
        "console_url": state.get("console_url"),
        "env_var": deployment.env_var,
        "has_token": deployment.token_secret_id is not None,
        "created_at": deployment.created_at.isoformat(),
        "updated_at": deployment.updated_at.isoformat(),
    }


def _load(session: Session, deployment_id: uuid.UUID, org_id: uuid.UUID) -> Deployment:
    deployment = session.get(Deployment, deployment_id)
    if deployment is None or deployment.org_id != org_id:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


@router.get("/providers")
def providers(
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    return list_providers()


@router.get("")
async def list_deployments(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    rows = session.exec(
        select(Deployment)
        .where(Deployment.org_id == agent_auth.org.id)
        .order_by(col(Deployment.created_at).desc())
    ).all()
    # One credential per account+placement for the whole page, not per row.
    cache: dict = {}
    return [_serialize(await refresh_status(session, d, cache)) for d in rows]


@router.post("", status_code=201)
async def create_deployment(
    body: CreateDeploymentRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, "deployments:manage")

    enabled, reason = provider_enabled(body.provider)
    if not enabled:
        raise HTTPException(status_code=400, detail=reason)
    provider = get_provider(body.provider)
    assert provider is not None

    connection = None
    if provider.connection_provider:
        if body.connection_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Deploying to {provider.display_name} needs a connected "
                f"{provider.connection_provider} account.",
            )
        connection = load_connection(session, body.connection_id, org_id=agent_auth.org.id)
        if connection is None or connection.provider != provider.connection_provider:
            raise HTTPException(status_code=404, detail="Connected account not found")
        if agent_auth.user is None or connection.user_id != agent_auth.user.id:
            # A connection is a personal grant; borrowing someone else's would
            # let one member deploy under another member's cloud identity.
            raise HTTPException(status_code=404, detail="Connected account not found")

    missing = [
        field.label
        for field in provider.config_fields
        if field.required and not (body.provider_config.get(field.key) or field.default)
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"{provider.display_name} needs: {', '.join(missing)}.",
        )

    # Declared defaults are filled in here so the stored config is the whole
    # truth, rather than a partial one that would behave differently if a
    # default changed later.
    resolved_config = {
        field.key: (body.provider_config.get(field.key) or field.default).strip()
        for field in provider.config_fields
    }

    probe_target = ProviderTarget(config=resolved_config)
    if connection is not None:
        # Credentials are resolved through the same path a real operation
        # uses, so "ready" means ready rather than merely "configured".
        probe = Deployment(
            org_id=agent_auth.org.id,
            name=body.name,
            slug="probe",
            provider=body.provider,
            connection_id=connection.id,
            config_json=json.dumps(resolved_config),
            package_zip=b"",
        )
        try:
            probe_target = await resolve_target(session, probe)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    ok, unavailable_reason = await provider.available(probe_target)
    if not ok:
        raise HTTPException(
            status_code=503,
            detail=f"Provider '{body.provider}' is not ready: {unavailable_reason}",
        )

    project = session.get(OpenAPIProject, body.project_id)
    if project is None or project.org_id != agent_auth.org.id:
        raise HTTPException(status_code=404, detail="OpenAPI project not found")

    definition, base_url, result, auth, _warnings = _resolve_compilation(project, body.compile)

    slug = slugify(body.name)
    _filename, package_zip = build_server_package(
        name=body.name,
        base_url=base_url,
        token_header=auth.token_header,
        token_format=auth.token_format,
        tools=[ct.tool for ct in result.tools],
        api_title=definition.title,
        api_version=definition.version,
    )

    deployment = Deployment(
        org_id=agent_auth.org.id,
        project_id=project.id,
        name=body.name,
        slug=slug,
        provider=body.provider,
        connection_id=connection.id if connection else None,
        config_json=json.dumps(resolved_config),
        status="queued",
        package_zip=package_zip,
        tool_count=len(result.tools),
        env_var=env_var_for(slug) if auth.token_header else None,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    session.add(deployment)
    session.flush()

    if body.token and deployment.env_var:
        secret = upsert_secret(
            session,
            org_id=agent_auth.org.id,
            kind="deployment_token",
            ref=f"deployments/{agent_auth.org.id}/{deployment.id}/token",
            value=body.token,
            secret_id=None,
        )
        deployment.token_secret_id = secret.id
        session.add(deployment)

    ip, user_agent = request_meta(request)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="deployment.created",
        summary=f"Deployment '{body.name}' created on {body.provider}",
        target_type="deployment",
        target_id=str(deployment.id),
        metadata={
            "provider": body.provider,
            "project_id": str(project.id),
            "tool_count": len(result.tools),
            # Placement is recorded, credentials are not: an audit reader needs
            # to know which account something landed in.
            "config": resolved_config,
        },
        ip=ip,
        user_agent=user_agent,
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(deployment)

    background_tasks.add_task(run_deployment, deployment.id)
    return _serialize(deployment)


@router.get("/{deployment_id}")
async def get_deployment(
    deployment_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    deployment = _load(session, deployment_id, agent_auth.org.id)
    return _serialize(await refresh_status(session, deployment))


@router.get("/{deployment_id}/logs")
async def deployment_logs(
    deployment_id: uuid.UUID,
    tail: int = Query(default=100, ge=1, le=2000),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    deployment = _load(session, deployment_id, agent_auth.org.id)
    provider = get_provider(deployment.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail="Provider is not available")
    try:
        target = await resolve_target(session, deployment)
        logs = await provider.logs(json.loads(deployment.provider_state_json), target, tail=tail)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"logs": logs}


async def _lifecycle(
    action: str,
    deployment_id: uuid.UUID,
    session: Session,
    agent_auth: AgentAuth,
    request: Request,
) -> dict:
    ensure_agent_can(session, agent_auth, "deployments:manage")
    deployment = _load(session, deployment_id, agent_auth.org.id)
    provider = get_provider(deployment.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail="Provider is not available")
    state = json.loads(deployment.provider_state_json)
    try:
        target = await resolve_target(session, deployment)
        if action == "stop":
            await provider.stop(state, target)
            deployment.status = "stopped"
        else:
            await provider.start(state, target)
            deployment.status = "running"
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    deployment.error = None
    session.add(deployment)
    ip, user_agent = request_meta(request)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action=f"deployment.{action}ped" if action == "stop" else "deployment.started",
        summary=f"Deployment '{deployment.name}' {action}ped"
        if action == "stop"
        else f"Deployment '{deployment.name}' started",
        target_type="deployment",
        target_id=str(deployment.id),
        ip=ip,
        user_agent=user_agent,
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(deployment)
    return _serialize(deployment)


@router.post("/{deployment_id}/stop")
async def stop_deployment(
    deployment_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    return await _lifecycle("stop", deployment_id, session, agent_auth, request)


@router.post("/{deployment_id}/start")
async def start_deployment(
    deployment_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    return await _lifecycle("start", deployment_id, session, agent_auth, request)


@router.delete("/{deployment_id}", status_code=204)
async def delete_deployment(
    deployment_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    ensure_agent_can(session, agent_auth, "deployments:manage")
    deployment = _load(session, deployment_id, agent_auth.org.id)

    provider = get_provider(deployment.provider)
    if provider is not None:
        try:
            target = await resolve_target(session, deployment)
            await provider.remove(json.loads(deployment.provider_state_json), target)
        except ProviderError as exc:
            # Removal is best-effort: never leave the row stuck because the
            # daemon is down; the container is labeled for manual cleanup.
            record_audit(
                session,
                org_id=agent_auth.org.id,
                action="deployment.cleanup_failed",
                summary=f"Provider cleanup failed for '{deployment.name}': {exc}"[:300],
                target_type="deployment",
                target_id=str(deployment.id),
                **actor_from_agent_auth(agent_auth),
            )

    if deployment.token_secret_id:
        delete_secret(session, deployment.token_secret_id)
        deployment.token_secret_id = None
        session.add(deployment)
        session.flush()

    name = deployment.name
    session.delete(deployment)
    ip, user_agent = request_meta(request)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="deployment.deleted",
        summary=f"Deployment '{name}' deleted",
        target_type="deployment",
        target_id=str(deployment_id),
        ip=ip,
        user_agent=user_agent,
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
