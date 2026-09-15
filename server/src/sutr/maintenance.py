"""Periodic housekeeping: prune expired rows that previously grew forever.

Runs in-process next to the tool-cache refresh loop. Every sweep is
best-effort — a failure is logged and retried on the next interval.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, col, select

from sutr import db
from sutr.models.consumed_event import ConsumedEvent
from sutr.models.google_login_state import GoogleLoginState
from sutr.models.idempotency_key import IdempotencyKey
from sutr.models.log import LogEntry
from sutr.models.oauth_revoked_token import OAuthRevokedToken
from sutr.models.org import Org
from sutr.models.outbox_event import PUBLISHED, OutboxEvent
from sutr.models.tool_approval_request import ToolApprovalRequest

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 3600
GOOGLE_LOGIN_STATE_TTL = timedelta(hours=1)
# How long a published event stays queryable. Long enough to answer "did that
# event get out?" during an incident; short enough that the outbox stays a
# queue. A dead-lettered event is never pruned — it is a fact the platform
# failed to announce, and losing it would hide the failure.
PUBLISHED_EVENT_RETENTION = timedelta(days=7)
# Deployment health/metering sampling cadence. Kept short enough that a stopped
# container is noticed quickly, long enough not to spam the docker daemon.
DEPLOYMENT_MONITOR_INTERVAL_SECONDS = 300
DEPLOYMENT_SAMPLE_MINUTES = DEPLOYMENT_MONITOR_INTERVAL_SECONDS // 60


def run_maintenance_sweep() -> dict[str, int]:
    """One synchronous sweep. Returns counts per task (also used by tests)."""
    counts = {
        "revoked_tokens_pruned": 0,
        "approvals_expired": 0,
        "google_states_pruned": 0,
        "logs_pruned": 0,
        "idempotency_keys_pruned": 0,
        "published_events_pruned": 0,
        "consumed_records_pruned": 0,
    }
    now = datetime.utcnow()  # naive UTC — matches the columns it compares against

    with Session(db.engine) as session:
        # Expired entries in the revocation denylist are unusable tokens anyway.
        expired_tokens = session.exec(
            select(OAuthRevokedToken).where(OAuthRevokedToken.expires_at < int(time.time()))
        ).all()
        for row in expired_tokens:
            session.delete(row)
        counts["revoked_tokens_pruned"] = len(expired_tokens)

        # Materialize the lazy "expired" state so status filters stop reporting
        # long-dead requests as pending.
        stale = session.exec(
            select(ToolApprovalRequest)
            .where(ToolApprovalRequest.status == "pending")
            .where(col(ToolApprovalRequest.expires_at) <= now)
        ).all()
        for req in stale:
            req.status = "expired"
            session.add(req)
        counts["approvals_expired"] = len(stale)

        cutoff = datetime.now(timezone.utc) - GOOGLE_LOGIN_STATE_TTL
        old_states = session.exec(
            select(GoogleLoginState).where(col(GoogleLoginState.created_at) < cutoff)
        ).all()
        for row in old_states:
            session.delete(row)
        counts["google_states_pruned"] = len(old_states)

        # Per-org tool-call log retention. The audit trail (audit_event) and the
        # metering ledger (usage_event) are deliberately exempt — they are the
        # durable compliance and billing records and are never pruned.
        retention_orgs = session.exec(
            select(Org).where(col(Org.log_retention_days).is_not(None))
        ).all()
        for org in retention_orgs:
            if not org.log_retention_days or org.log_retention_days <= 0:
                continue
            log_cutoff = now - timedelta(days=org.log_retention_days)
            result = session.execute(
                sa_delete(LogEntry)
                .where(LogEntry.org_id == org.id)  # type: ignore[arg-type]
                .where(LogEntry.timestamp < log_cutoff)  # type: ignore[arg-type]
            )
            counts["logs_pruned"] += result.rowcount or 0

        # Idempotency keys past their retention window. Keeping them forever
        # would grow a table whose only purpose is a 24-hour memory.
        expired_keys = session.execute(
            sa_delete(IdempotencyKey).where(IdempotencyKey.expires_at < now)  # type: ignore[arg-type]
        )
        counts["idempotency_keys_pruned"] = expired_keys.rowcount or 0

        # Published events, once they are older than the audit window. The
        # outbox is a queue, not the audit log — `audit_event` is the record
        # that is kept — but published rows are worth retaining briefly so
        # "did the event get out?" is answerable after the fact.
        published_cutoff = now - PUBLISHED_EVENT_RETENTION
        published = session.execute(
            sa_delete(OutboxEvent)
            .where(OutboxEvent.state == PUBLISHED)  # type: ignore[arg-type]
            .where(OutboxEvent.published_at < published_cutoff)  # type: ignore[arg-type]
        )
        counts["published_events_pruned"] = published.rowcount or 0

        # Consumption records for events that no longer exist. Kept as long as
        # the event itself, since their only job is to recognise a redelivery.
        consumed = session.execute(
            sa_delete(ConsumedEvent).where(ConsumedEvent.consumed_at < published_cutoff)  # type: ignore[arg-type]
        )
        counts["consumed_records_pruned"] = consumed.rowcount or 0

        session.commit()

    return counts


async def maintenance_loop() -> None:
    while True:
        try:
            counts = await asyncio.to_thread(run_maintenance_sweep)
            if any(counts.values()):
                logger.info("maintenance sweep: %s", counts)
        except Exception:
            logger.exception("maintenance sweep failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)


async def sweep_deployments() -> dict[str, int]:
    """Reconcile deployment status with providers, meter runtime, set gauges.

    Async (not run in a thread) because provider APIs are async. Each running
    deployment observed by a sweep accrues DEPLOYMENT_SAMPLE_MINUTES of metered
    runtime — sampling, so a crash between sweeps under-bills rather than
    over-bills.
    """
    from sutr.models.deployment import Deployment
    from sutr.observability.metrics import set_deployment_counts
    from sutr.services.deployments import refresh_status
    from sutr.services.metering import record_deployment_runtime

    counts: dict[str, int] = {}
    reconciled = 0
    metered = 0

    with Session(db.engine) as session:
        deployments = session.exec(select(Deployment)).all()
        for deployment in deployments:
            previous = deployment.status
            try:
                deployment = await refresh_status(session, deployment)
            except Exception:
                logger.exception("deployment status refresh failed: %s", deployment.id)
            if deployment.status != previous:
                reconciled += 1
                logger.info("deployment %s: %s -> %s", deployment.id, previous, deployment.status)
            counts[deployment.status] = counts.get(deployment.status, 0) + 1
            if deployment.status == "running":
                record_deployment_runtime(
                    session,
                    org_id=deployment.org_id,
                    deployment_id=deployment.id,
                    minutes=DEPLOYMENT_SAMPLE_MINUTES,
                )
                metered += 1
        session.commit()

    set_deployment_counts(counts)
    return {"reconciled": reconciled, "runtime_metered": metered, **counts}


async def deployment_monitor_loop() -> None:
    while True:
        await asyncio.sleep(DEPLOYMENT_MONITOR_INTERVAL_SECONDS)
        try:
            result = await sweep_deployments()
            if result.get("reconciled") or result.get("runtime_metered"):
                logger.info("deployment sweep: %s", result)
        except Exception:
            logger.exception("deployment sweep failed")
