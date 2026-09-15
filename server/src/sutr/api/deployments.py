"""Deployment endpoints: run generated MCP servers on a provider.

Create snapshots the generated package onto the row, stores the runtime token
through the secrets backend (never in the package or the image), and builds
in a background task. Reads reconcile stored status with the provider.
"""

import json
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from sutr.api.openapi_projects import CompileRequest, resolve_compilation
from sutr.authz import ensure_agent_can
from sutr.common import idempotency
from sutr.connections.store import load_connection
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.deploy.base import ProviderError, ProviderTarget
from sutr.deploy.credentials import deployment_config, resolve_target
from sutr.deploy.drift import detect as detect_drift
from sutr.deploy.registry import get_provider, list_providers, provider_enabled
from sutr.generation import artifacts
from sutr.models.deployment import Deployment
from sutr.models.deployment_revision import (
    ORIGIN_ROLLBACK,
    ORIGIN_UPDATE,
    OUTCOME_ACTIVE,
    OUTCOME_PENDING,
    OUTCOME_SUPERSEDED,
)
from sutr.models.openapi_project import OpenAPIProject
from sutr.openapi.packaging import build_server_package, env_var_for, slugify
from sutr.secrets.records import delete_secret, upsert_secret
from sutr.services.audit import actor_from_agent_auth, record_audit, request_meta
from sutr.services.deployments import (
    collect_metrics,
    get_revision,
    list_revisions,
    next_revision_number,
    record_revision,
    refresh_status,
    rollback_deployment,
    run_deployment,
    update_deployment,
)

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


class CreateDeploymentRequest(BaseModel):
    # Either a project to compile now, or a stored runtime artifact to deploy.
    # Exactly one, checked below: two sources for the same bytes would leave
    # the caller unsure which one actually shipped.
    project_id: uuid.UUID | None = None
    # LLD §3.6: only validated artifacts are deployable. This is where that is
    # enforced — an artifact whose validation report says `rejected` is refused
    # here, and the bytes deployed are the bytes that passed the gate rather
    # than a rebuild that has not been through it.
    artifact_id: uuid.UUID | None = None
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


class UpdateDeploymentRequest(BaseModel):
    """Deploy a new revision: a recompile of the source project, or an artifact.

    Passing `artifact_id` updates to a stored, validated build instead of
    recompiling — which is what a provider wants when the artifact they tested
    is the one they mean to promote.
    """

    compile: CompileRequest = CompileRequest()
    artifact_id: uuid.UUID | None = None


class RollbackDeploymentRequest(BaseModel):
    """Restore a retained revision. None → the most recent one that ran."""

    revision: int | None = None


def _load_deployable_artifact(session: Session, artifact_id: uuid.UUID, org_id: uuid.UUID):
    """The validation gate (LLD §3.6: *only validated artifacts deployable*)."""
    artifact = artifacts.get(session, artifact_id, org_id)
    if artifact is None:
        # Same answer for "no such artifact" and "another tenant's artifact".
        raise HTTPException(status_code=404, detail="Runtime artifact not found")
    ok, reason = artifacts.deployable(artifact)
    if not ok:
        # 409 rather than 400: the request is well formed, and the artifact's
        # state is what refuses it.
        raise HTTPException(status_code=409, detail=reason)
    return artifact


def _artifact_secret_env_var(artifact) -> str | None:
    """The credential variable the artifact's package actually reads."""
    manifest = json.loads(artifact.manifest_json or "{}")
    for entry in manifest.get("environment") or []:
        if entry.get("secret") and entry.get("required"):
            return entry.get("name")
    return None


def _serialize(deployment: Deployment) -> dict:
    state = json.loads(deployment.provider_state_json)
    return {
        "id": str(deployment.id),
        "artifact_id": str(deployment.artifact_id) if deployment.artifact_id else None,
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
        "current_revision": deployment.current_revision,
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


@router.post("", status_code=201, response_model=None)
async def create_deployment(
    body: CreateDeploymentRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict | JSONResponse:
    """Create a deployment.

    Optionally idempotent: send `Idempotency-Key` and a retry after a timeout
    returns the original deployment rather than creating a second one. Build
    prompt §76 requires this for runtime deployment, and a deployment is
    exactly the kind of expensive, slow, retry-prone operation where a
    duplicate is painful — two containers, two bills, one intent.

    Without the header the endpoint behaves exactly as before.
    """
    ensure_agent_can(session, agent_auth, "deployments:manage")

    claimed = idempotency.claim(
        session,
        org_id=agent_auth.org.id,
        endpoint="POST /api/deployments",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
    )
    if isinstance(claimed, idempotency.Replay):
        return JSONResponse(status_code=claimed.status_code, content=claimed.body)

    try:
        result = await _create_deployment(body, request, background_tasks, session, agent_auth)
    except Exception:
        # A failed attempt must not hold the key for a day; the caller should be
        # able to retry and have it actually run.
        idempotency.release(session, claimed)
        raise
    idempotency.complete(session, claimed, status_code=201, body=result)
    return result


async def _create_deployment(
    body: CreateDeploymentRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session,
    agent_auth: AgentAuth,
) -> dict:

    if (body.project_id is None) == (body.artifact_id is None):
        raise HTTPException(
            status_code=400,
            detail="Provide either project_id (compile now) or artifact_id (deploy a build).",
        )

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

    artifact = None
    if body.artifact_id is not None:
        artifact = _load_deployable_artifact(session, body.artifact_id, agent_auth.org.id)
        project_id = artifact.project_id
        slug = artifact.slug
        package_zip = artifact.package_zip
        tool_count = artifact.tool_count
        # The env var comes from the artifact's own manifest rather than from
        # the deployment name: the package reads the variable the generator
        # named, and deriving it from a name the user typed here would inject
        # the credential under a name nothing reads.
        env_var = _artifact_secret_env_var(artifact)
    else:
        project = session.get(OpenAPIProject, body.project_id)
        if project is None or project.org_id != agent_auth.org.id:
            raise HTTPException(status_code=404, detail="OpenAPI project not found")

        definition, base_url, result, auth, _warnings = resolve_compilation(project, body.compile)

        project_id = project.id
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
        tool_count = len(result.tools)
        env_var = env_var_for(slug) if auth.token_header else None

    deployment = Deployment(
        org_id=agent_auth.org.id,
        project_id=project_id,
        artifact_id=artifact.id if artifact else None,
        name=body.name,
        slug=slug,
        provider=body.provider,
        connection_id=connection.id if connection else None,
        config_json=json.dumps(resolved_config),
        status="queued",
        package_zip=package_zip,
        tool_count=tool_count,
        env_var=env_var,
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
            "project_id": str(project_id) if project_id else None,
            "artifact_id": str(artifact.id) if artifact else None,
            "tool_count": tool_count,
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


# ── Revisions: update, rollback, history (build prompt §36, ADR-016) ─────────


def _serialize_revision(entry) -> dict:
    return {
        "revision": entry.revision,
        "origin": entry.origin,
        "restored_from_revision": entry.restored_from_revision,
        "outcome": entry.outcome,
        "error": entry.error,
        "package_sha256": entry.package_sha256,
        "tool_count": entry.tool_count,
        "url": entry.url,
        "created_at": entry.created_at.isoformat(),
    }


@router.get("/{deployment_id}/revisions")
def deployment_revisions(
    deployment_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> list[dict]:
    """Every version this deployment has run, newest first."""
    ensure_agent_can(session, agent_auth, "deployments:manage")
    _load(session, deployment_id, agent_auth.org.id)
    return [_serialize_revision(entry) for entry in list_revisions(session, deployment_id)]


@router.post("/{deployment_id}/update")
async def update_deployment_endpoint(
    deployment_id: uuid.UUID,
    body: "UpdateDeploymentRequest",
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Recompile the source project and apply the result as a new revision.

    This is not a delete followed by a create: the deployment keeps its
    identity, its URL, and its history, and the previous revision's artifact is
    retained so it can be rolled back to.
    """
    ensure_agent_can(session, agent_auth, "deployments:manage")
    deployment = _load(session, deployment_id, agent_auth.org.id)

    provider = get_provider(deployment.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail="The provider is not available.")
    if not provider.supports_update:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The {provider.display_name} provider cannot update a deployment in place. "
                "Delete it and create a new one."
            ),
        )
    artifact = None
    if body.artifact_id is not None:
        artifact = _load_deployable_artifact(session, body.artifact_id, agent_auth.org.id)
        package_zip = artifact.package_zip
        tool_count = artifact.tool_count
        deployment.artifact_id = artifact.id
        # An artifact generated for a different name reads its credential from
        # a different variable, so the deployment follows the artifact.
        deployment.env_var = _artifact_secret_env_var(artifact) or deployment.env_var
    else:
        if deployment.project_id is None:
            raise HTTPException(
                status_code=400,
                detail="This deployment has no source project, so there is nothing to recompile.",
            )
        project = session.get(OpenAPIProject, deployment.project_id)
        if project is None or project.org_id != agent_auth.org.id:
            raise HTTPException(
                status_code=404,
                detail="The source OpenAPI project no longer exists, so it cannot be recompiled.",
            )

        definition, base_url, result, auth, _warnings = resolve_compilation(project, body.compile)
        _, package_zip = build_server_package(
            name=deployment.name,
            base_url=base_url,
            token_header=auth.token_header,
            token_format=auth.token_format,
            tools=[ct.tool for ct in result.tools],
            api_title=definition.title,
            api_version=definition.version,
        )
        tool_count = len(result.tools)

    revision = next_revision_number(session, deployment_id)
    deployment.tool_count = tool_count
    session.add(deployment)
    record_revision(
        session,
        deployment,
        revision=revision,
        origin=ORIGIN_UPDATE,
        package_zip=package_zip,
        artifact_id=artifact.id if artifact else None,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    ip, user_agent = request_meta(request)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="deployment.update_requested",
        summary=f"Deployment '{deployment.name}' update to revision {revision} requested",
        target_type="deployment",
        target_id=str(deployment.id),
        metadata={
            "revision": revision,
            "tool_count": tool_count,
            "artifact_id": str(artifact.id) if artifact else None,
        },
        ip=ip,
        user_agent=user_agent,
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(deployment)

    background_tasks.add_task(update_deployment, deployment.id, revision)
    return {**_serialize(deployment), "pending_revision": revision}


@router.post("/{deployment_id}/rollback")
async def rollback_deployment_endpoint(
    deployment_id: uuid.UUID,
    body: "RollbackDeploymentRequest",
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Re-apply a retained revision.

    The stored package is re-run rather than rebuilt from source, so a rollback
    restores what actually ran, not what the source happens to produce today.
    """
    ensure_agent_can(session, agent_auth, "deployments:manage")
    deployment = _load(session, deployment_id, agent_auth.org.id)

    provider = get_provider(deployment.provider)
    if provider is None:
        raise HTTPException(status_code=400, detail="The provider is not available.")

    target_revision = body.revision
    if target_revision is None:
        # Default: the most recent revision that actually ran before this one.
        previous = [
            entry
            for entry in list_revisions(session, deployment_id)
            if entry.revision < deployment.current_revision
            and entry.outcome in (OUTCOME_ACTIVE, OUTCOME_SUPERSEDED)
        ]
        if not previous:
            raise HTTPException(
                status_code=400,
                detail="There is no earlier revision to roll back to.",
            )
        target_revision = previous[0].revision

    source = get_revision(session, deployment_id, target_revision)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Revision {target_revision} not found")
    if source.outcome == OUTCOME_PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Revision {target_revision} never finished deploying.",
        )

    revision = next_revision_number(session, deployment_id)
    deployment.tool_count = source.tool_count
    session.add(deployment)
    record_revision(
        session,
        deployment,
        revision=revision,
        origin=ORIGIN_ROLLBACK,
        package_zip=source.package_zip,
        artifact_id=source.artifact_id,
        restored_from_revision=target_revision,
        created_by_user_id=agent_auth.user.id if agent_auth.user else None,
    )
    ip, user_agent = request_meta(request)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="deployment.rollback_requested",
        summary=(
            f"Deployment '{deployment.name}' rollback to revision {target_revision} "
            f"requested (as revision {revision})"
        ),
        target_type="deployment",
        target_id=str(deployment.id),
        metadata={"revision": revision, "restored_from_revision": target_revision},
        ip=ip,
        user_agent=user_agent,
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(deployment)

    background_tasks.add_task(rollback_deployment, deployment.id, revision)
    return {
        **_serialize(deployment),
        "pending_revision": revision,
        "restoring_revision": target_revision,
    }


@router.get("/{deployment_id}/metrics")
async def deployment_metrics(
    deployment_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Runtime measurements, as far as the provider reports them.

    Fields the provider does not expose come back as null with an
    `unavailable_reason` — a missing number reads as "not reported", never as
    zero.
    """
    ensure_agent_can(session, agent_auth, "deployments:manage")
    deployment = _load(session, deployment_id, agent_auth.org.id)
    metrics = await collect_metrics(session, deployment)
    return {
        "deployment_id": str(deployment.id),
        "provider": deployment.provider,
        "status": deployment.status,
        "revision": deployment.current_revision,
        **metrics.as_dict(),
    }


@router.get("/{deployment_id}/drift")
async def deployment_drift(
    deployment_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What the provider actually has, against what the platform declared.

    LLD §5.7 asks for drift detection alongside version history and one-command
    rollback, which already exist here. A clean report is the common case and
    the useful one: it is what makes an unclean one worth reading.

    This is detection, not reconciliation — nothing is corrected as a side
    effect of looking. Correcting drift is `POST /rollback` or `POST /update`,
    which are audited actions a person takes.
    """
    ensure_agent_can(session, agent_auth, "deployments:manage")
    deployment = _load(session, deployment_id, agent_auth.org.id)
    report = await detect_drift(session, deployment)
    return {
        "deployment_id": str(deployment.id),
        "provider": deployment.provider,
        "status": deployment.status,
        "revision": deployment.current_revision,
        **report.as_dict(),
    }
