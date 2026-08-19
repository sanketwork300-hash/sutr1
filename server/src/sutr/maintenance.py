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
from sutr.models.google_login_state import GoogleLoginState
from sutr.models.log import LogEntry
from sutr.models.oauth_revoked_token import OAuthRevokedToken
from sutr.models.org import Org
from sutr.models.tool_approval_request import ToolApprovalRequest

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 3600
GOOGLE_LOGIN_STATE_TTL = timedelta(hours=1)


def run_maintenance_sweep() -> dict[str, int]:
    """One synchronous sweep. Returns counts per task (also used by tests)."""
    counts = {
        "revoked_tokens_pruned": 0,
        "approvals_expired": 0,
        "google_states_pruned": 0,
        "logs_pruned": 0,
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

        # Per-org tool-call log retention. The audit trail (audit_event) is
        # deliberately exempt — it is the durable record and is never pruned.
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
