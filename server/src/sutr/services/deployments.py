"""Deployment orchestration: background build/run and status synchronization.

The API layer creates the Deployment row (status="queued") with the package
zip snapshotted, then schedules `run_deployment` as a background task. That
task drives the provider and lands the row in "running" or "failed" — it uses
its own short-lived sessions because it outlives the request.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, col, select

from sutr import db, events
from sutr.deploy.base import DeploySpec, ProviderError, ProviderMetrics, ProviderStatus
from sutr.deploy.credentials import resolve_target
from sutr.deploy.registry import get_provider
from sutr.events import topics
from sutr.models.deployment import Deployment
from sutr.models.deployment_revision import (
    ORIGIN_CREATE,
    ORIGIN_ROLLBACK,
    ORIGIN_UPDATE,
    OUTCOME_ACTIVE,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    OUTCOME_SUPERSEDED,
    DeploymentRevision,
)
from sutr.secrets.records import get_secret_value
from sutr.services.audit import record_audit

logger = logging.getLogger(__name__)


def _set_status(deployment_id: uuid.UUID, **fields) -> None:
    with Session(db.engine) as session:
        deployment = session.get(Deployment, deployment_id)
        if deployment is None:
            return
        for key, value in fields.items():
            setattr(deployment, key, value)
        deployment.updated_at = datetime.utcnow()
        session.add(deployment)
        session.commit()


async def run_deployment(deployment_id: uuid.UUID) -> None:
    """Build and start a queued deployment. Safe to fire-and-forget.

    Revision 1 is opened here rather than at create time so a deployment that
    never reached a provider still has an honest history: one revision, failed.
    """
    with Session(db.engine) as session:
        deployment = session.get(Deployment, deployment_id)
        if deployment is None:
            return
        if get_revision(session, deployment_id, 1) is None:
            record_revision(
                session,
                deployment,
                revision=1,
                origin=ORIGIN_CREATE,
                package_zip=deployment.package_zip,
                artifact_id=deployment.artifact_id,
                created_by_user_id=deployment.created_by_user_id,
            )
            session.commit()
    await _apply_revision(deployment_id, 1, origin=ORIGIN_CREATE)


def _emit(org_id: uuid.UUID, deployment_id: uuid.UUID, event_type: str, payload: dict) -> None:
    """Announce a runtime fact (LLD §3.1: Runtime Manager → Registry).

    Its own session and its own commit: the provider call already happened, so
    there is no state change left to bind the event to. Failing to record the
    event must not fail a deployment that succeeded, so it is logged instead.
    """
    try:
        with Session(db.engine) as session:
            events.publish(
                session,
                event_type,
                tenant_id=org_id,
                resource_id=str(deployment_id),
                producer="runtime-manager",
                payload=payload,
            )
            session.commit()
    except Exception:
        logger.warning(
            "recording %s for deployment %s failed", event_type, deployment_id, exc_info=True
        )


def _audit_outcome(
    org_id: uuid.UUID, deployment_id: uuid.UUID, name: str, action: str, detail: str
) -> None:
    with Session(db.engine) as session:
        record_audit(
            session,
            org_id=org_id,
            action=action,
            summary=f"Deployment '{name}': {detail}"[:300],
            actor_type="system",
            target_type="deployment",
            target_id=str(deployment_id),
        )
        session.commit()


async def refresh_status(
    session: Session, deployment: Deployment, cache: dict | None = None
) -> Deployment:
    """Best-effort reconciliation of the stored status with the provider.

    `cache` lets one request reuse a credential across deployments that share
    an account and placement; it must not outlive that request.
    """
    if deployment.status in ("queued", "building", "failed"):
        return deployment
    provider = get_provider(deployment.provider)
    if provider is None:
        return deployment
    try:
        target = await resolve_target(session, deployment, cache)
        live: ProviderStatus = await provider.status(
            json.loads(deployment.provider_state_json), target
        )
    except Exception:
        # Reconciliation is best-effort by design: a cloud that is briefly
        # unreachable, or an authorization that needs renewing, must not
        # rewrite the stored status into something wrong.
        return deployment

    mapped = {
        "running": "running",
        "stopped": "stopped",
        "not_found": "failed",
        "error": "failed",
    }.get(live.state, deployment.status)
    if mapped != deployment.status:
        deployment.status = mapped
        deployment.error = live.detail if mapped == "failed" else None
        deployment.updated_at = datetime.utcnow()
        session.add(deployment)
        session.commit()
        session.refresh(deployment)
    return deployment


# ── Revisions (build prompt §36, ADR-016) ────────────────────────────────────


def package_digest(package_zip: bytes) -> str:
    return hashlib.sha256(package_zip).hexdigest()


def record_revision(
    session: Session,
    deployment: Deployment,
    *,
    revision: int,
    origin: str,
    package_zip: bytes,
    artifact_id: uuid.UUID | None = None,
    restored_from_revision: int | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> DeploymentRevision:
    """Open a new revision. It stays `pending` until the provider applies it."""
    entry = DeploymentRevision(
        deployment_id=deployment.id,
        org_id=deployment.org_id,
        revision=revision,
        origin=origin,
        restored_from_revision=restored_from_revision,
        outcome=OUTCOME_PENDING,
        package_zip=package_zip,
        package_sha256=package_digest(package_zip),
        artifact_id=artifact_id,
        config_json=deployment.config_json,
        tool_count=deployment.tool_count,
        created_by_user_id=created_by_user_id,
    )
    session.add(entry)
    return entry


def list_revisions(session: Session, deployment_id: uuid.UUID) -> list[DeploymentRevision]:
    return list(
        session.exec(
            select(DeploymentRevision)
            .where(DeploymentRevision.deployment_id == deployment_id)
            .order_by(col(DeploymentRevision.revision).desc())
        ).all()
    )


def get_revision(
    session: Session, deployment_id: uuid.UUID, revision: int
) -> DeploymentRevision | None:
    return session.exec(
        select(DeploymentRevision)
        .where(DeploymentRevision.deployment_id == deployment_id)
        .where(DeploymentRevision.revision == revision)
    ).first()


def next_revision_number(session: Session, deployment_id: uuid.UUID) -> int:
    highest = session.exec(
        select(func.max(DeploymentRevision.revision)).where(
            DeploymentRevision.deployment_id == deployment_id
        )
    ).one()
    return int(highest or 0) + 1


def _settle_revision(
    deployment_id: uuid.UUID,
    revision: int,
    *,
    outcome: str,
    state: dict | None = None,
    error: str | None = None,
) -> None:
    with Session(db.engine) as session:
        entry = get_revision(session, deployment_id, revision)
        if entry is None:
            return
        entry.outcome = outcome
        entry.error = error
        if state is not None:
            entry.provider_state_json = json.dumps(state)
            entry.url = state.get("url")
        session.add(entry)
        if outcome == OUTCOME_ACTIVE:
            # Exactly one revision is active at a time.
            for other in list_revisions(session, deployment_id):
                if other.revision != revision and other.outcome == OUTCOME_ACTIVE:
                    other.outcome = OUTCOME_SUPERSEDED
                    session.add(other)
        session.commit()


async def _apply_revision(
    deployment_id: uuid.UUID,
    revision: int,
    *,
    origin: str,
) -> None:
    """Drive the provider for one revision and settle its outcome.

    Shared by create, update, and rollback: the three differ in which package
    they carry and what they are called, not in how they are applied. That is
    the point of ADR-016 — an update is a new revision of the same deployment,
    never a delete followed by a create.
    """
    with Session(db.engine) as session:
        deployment = session.get(Deployment, deployment_id)
        if deployment is None:
            return
        entry = get_revision(session, deployment_id, revision)
        if entry is None:
            return
        provider = get_provider(deployment.provider)
        if provider is None:
            _set_status(deployment_id, status="failed", error="Provider is not available.")
            _settle_revision(
                deployment_id, revision, outcome=OUTCOME_FAILED, error="Provider is not available."
            )
            return
        if origin != ORIGIN_CREATE and not provider.supports_update:
            reason = f"The {provider.display_name} provider cannot update a deployment in place."
            _set_status(deployment_id, error=reason)
            _settle_revision(deployment_id, revision, outcome=OUTCOME_FAILED, error=reason)
            return
        token = (
            get_secret_value(session, deployment.token_secret_id)
            if deployment.token_secret_id
            else None
        )
        try:
            target = await resolve_target(session, deployment)
        except ProviderError as exc:
            _set_status(deployment_id, status="failed", error=str(exc))
            _settle_revision(deployment_id, revision, outcome=OUTCOME_FAILED, error=str(exc))
            return
        spec = DeploySpec(
            deployment_id=deployment.id,
            org_id=deployment.org_id,
            name=deployment.name,
            slug=deployment.slug,
            package_zip=entry.package_zip,
            env={deployment.env_var: token} if (deployment.env_var and token) else {},
            target=target,
            revision=revision,
            previous_state=json.loads(deployment.provider_state_json or "{}"),
        )
        org_id = deployment.org_id
        name = deployment.name

    _set_status(deployment_id, status="building", error=None)
    action = {
        ORIGIN_CREATE: provider.deploy,
        ORIGIN_UPDATE: provider.update,
        ORIGIN_ROLLBACK: provider.rollback,
    }[origin]
    try:
        state = await action(spec)
    except ProviderError as exc:
        logger.warning("deployment %s revision %s failed: %s", deployment_id, revision, exc)
        _set_status(deployment_id, status="failed", error=str(exc))
        _settle_revision(deployment_id, revision, outcome=OUTCOME_FAILED, error=str(exc))
        _audit_outcome(org_id, deployment_id, name, "deployment.failed", str(exc))
        _emit(
            org_id,
            deployment_id,
            topics.RUNTIME_FAILED,
            {"revision": revision, "error": str(exc)[:500], "name": name},
        )
        return
    except Exception:
        logger.exception("deployment %s revision %s crashed", deployment_id, revision)
        _set_status(deployment_id, status="failed", error="Unexpected deployment error.")
        _settle_revision(
            deployment_id, revision, outcome=OUTCOME_FAILED, error="Unexpected deployment error."
        )
        _audit_outcome(org_id, deployment_id, name, "deployment.failed", "unexpected error")
        _emit(
            org_id,
            deployment_id,
            topics.RUNTIME_FAILED,
            {"revision": revision, "error": "unexpected deployment error", "name": name},
        )
        return

    _set_status(
        deployment_id,
        status="running",
        url=state.get("url"),
        provider_state_json=json.dumps(state),
        current_revision=revision,
        error=None,
    )
    _settle_revision(deployment_id, revision, outcome=OUTCOME_ACTIVE, state=state)
    _audit_outcome(
        org_id,
        deployment_id,
        name,
        {
            ORIGIN_CREATE: "deployment.started",
            ORIGIN_UPDATE: "deployment.updated",
            ORIGIN_ROLLBACK: "deployment.rolled_back",
        }[origin],
        f"revision {revision} at {state.get('url') or ''}".strip(),
    )
    _emit(
        org_id,
        deployment_id,
        {
            ORIGIN_CREATE: topics.RUNTIME_DEPLOYED,
            ORIGIN_UPDATE: topics.RUNTIME_UPDATED,
            ORIGIN_ROLLBACK: topics.RUNTIME_ROLLED_BACK,
        }[origin],
        {"revision": revision, "url": state.get("url"), "name": name},
    )


async def update_deployment(deployment_id: uuid.UUID, revision: int) -> None:
    """Apply a new revision to a running deployment. Fire-and-forget."""
    await _apply_revision(deployment_id, revision, origin=ORIGIN_UPDATE)


async def rollback_deployment(deployment_id: uuid.UUID, revision: int) -> None:
    """Re-apply a retained revision. Fire-and-forget."""
    await _apply_revision(deployment_id, revision, origin=ORIGIN_ROLLBACK)


async def collect_metrics(session: Session, deployment: Deployment) -> ProviderMetrics:
    """Runtime measurements for one deployment, as far as its provider reports."""
    provider = get_provider(deployment.provider)
    if provider is None:
        return ProviderMetrics(
            source=deployment.provider,
            unavailable_reason="The provider is not available on this instance.",
        )
    if deployment.status in ("queued", "building", "failed"):
        return ProviderMetrics(
            source=provider.id,
            unavailable_reason=f"The deployment is {deployment.status}.",
        )
    try:
        target = await resolve_target(session, deployment)
        return await provider.metrics(json.loads(deployment.provider_state_json), target)
    except Exception as exc:
        # Metrics are observational: a provider that is briefly unreachable
        # must not turn into an error page.
        return ProviderMetrics(source=provider.id, unavailable_reason=str(exc))
