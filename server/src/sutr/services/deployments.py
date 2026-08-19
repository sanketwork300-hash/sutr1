"""Deployment orchestration: background build/run and status synchronization.

The API layer creates the Deployment row (status="queued") with the package
zip snapshotted, then schedules `run_deployment` as a background task. That
task drives the provider and lands the row in "running" or "failed" — it uses
its own short-lived sessions because it outlives the request.
"""

import json
import logging
import uuid
from datetime import datetime

from sqlmodel import Session

from sutr import db
from sutr.deploy.base import DeploySpec, ProviderError, ProviderStatus
from sutr.deploy.registry import get_provider
from sutr.models.deployment import Deployment
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
    """Build and start a queued deployment. Safe to fire-and-forget."""
    with Session(db.engine) as session:
        deployment = session.get(Deployment, deployment_id)
        if deployment is None:
            return
        provider = get_provider(deployment.provider)
        if provider is None:
            _set_status(deployment_id, status="failed", error="Provider is not available.")
            return
        token = (
            get_secret_value(session, deployment.token_secret_id)
            if deployment.token_secret_id
            else None
        )
        spec = DeploySpec(
            deployment_id=deployment.id,
            name=deployment.name,
            slug=deployment.slug,
            package_zip=deployment.package_zip,
            env={deployment.env_var: token} if (deployment.env_var and token) else {},
        )
        org_id = deployment.org_id
        name = deployment.name

    _set_status(deployment_id, status="building", error=None)
    try:
        state = await provider.deploy(spec)
    except ProviderError as exc:
        logger.warning("deployment %s failed: %s", deployment_id, exc)
        _set_status(deployment_id, status="failed", error=str(exc))
        _audit_outcome(org_id, deployment_id, name, "deployment.failed", str(exc))
        return
    except Exception:
        logger.exception("deployment %s crashed", deployment_id)
        _set_status(deployment_id, status="failed", error="Unexpected deployment error.")
        _audit_outcome(org_id, deployment_id, name, "deployment.failed", "unexpected error")
        return

    _set_status(
        deployment_id,
        status="running",
        url=state.get("url"),
        provider_state_json=json.dumps(state),
        error=None,
    )
    _audit_outcome(org_id, deployment_id, name, "deployment.started", state.get("url") or "")


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


async def refresh_status(session: Session, deployment: Deployment) -> Deployment:
    """Best-effort reconciliation of the stored status with the provider."""
    if deployment.status in ("queued", "building", "failed"):
        return deployment
    provider = get_provider(deployment.provider)
    if provider is None:
        return deployment
    try:
        live: ProviderStatus = await provider.status(json.loads(deployment.provider_state_json))
    except Exception:
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
