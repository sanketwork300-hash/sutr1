"""Quota evaluation — before execution, never after (ESDS LLD §5.1).

Two rules from the build prompt (§48) shape everything here:

1. *"Quota checks happen before invocation."* Evaluation runs inside the
   canonical pipeline's gate, ahead of dispatch, so an over-quota call never
   reaches a provider.
2. *"Rejected requests should not be billed as successful executions."* A
   rejection writes a usage row with `quantity=0` and outcome
   `quota_exceeded`, so the refusal is visible and auditable without becoming
   a billable execution.

Counts are derived from the usage ledger rather than kept in a counter table.
The ledger is already the durable, authoritative record of what happened, and
a second counter would be a second truth that could disagree with the bill.

Concurrency is the exception: an in-flight call is not in the ledger yet, so
it is tracked in process. That is correct for a single worker and honest about
what it is not — with several workers each has its own view, which is recorded
as a known limitation rather than papered over.
"""

import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from sutr.models.quota import (
    CONCURRENT_TOOL_CALLS,
    DAILY_DATA_TRANSFER_BYTES,
    DAILY_TOKENS,
    DAILY_TOOL_CALLS,
    MONTHLY_DATA_TRANSFER_BYTES,
    MONTHLY_TOKENS,
    MONTHLY_TOOL_CALLS,
    SCOPE_INTEGRATION,
    SCOPE_TENANT,
    SCOPE_TOOL,
    Quota,
)
from sutr.models.usage_event import KIND_TOOL_CALL, UsageEvent

logger = logging.getLogger(__name__)

# Which usage dimension each quota counts.
_CALL_KINDS = (DAILY_TOOL_CALLS, MONTHLY_TOOL_CALLS)
_TOKEN_KINDS = (DAILY_TOKENS, MONTHLY_TOKENS)
_TRANSFER_KINDS = (DAILY_DATA_TRANSFER_BYTES, MONTHLY_DATA_TRANSFER_BYTES)
_DAILY_KINDS = (DAILY_TOOL_CALLS, DAILY_TOKENS, DAILY_DATA_TRANSFER_BYTES)


@dataclass(frozen=True)
class QuotaVerdict:
    """The outcome of evaluating every quota that applies to one call."""

    allowed: bool
    kind: str | None = None
    scope: str | None = None
    limit: int | None = None
    used: int | None = None
    retry_after: int | None = None  # seconds until the window rolls over

    @property
    def message(self) -> str:
        if self.allowed:
            return "Within quota."
        if self.kind == CONCURRENT_TOOL_CALLS:
            return (
                f"Concurrency limit reached: {self.limit} simultaneous tool calls. "
                "Retry when an in-flight call finishes."
            )
        return (
            f"Quota '{self.kind}' exceeded for this {self.scope}: {self.used} of {self.limit} used."
        )

    def as_detail(self) -> dict:
        return {
            "error": "quota_exceeded",
            "message": self.message,
            "quota": self.kind,
            "scope": self.scope,
            "limit": self.limit,
            "used": self.used,
            "retry_after": self.retry_after,
        }


ALLOWED = QuotaVerdict(allowed=True)


# ── Windows ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _window_start(kind: str, now: datetime) -> datetime:
    if kind in _DAILY_KINDS:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _seconds_until_window_rolls(kind: str, now: datetime) -> int:
    if kind in _DAILY_KINDS:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(int((tomorrow - now).total_seconds()), 1)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    next_month = next_month.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((next_month - now).total_seconds()), 1)


# ── Concurrency ──────────────────────────────────────────────────────────────


class _ConcurrencyTracker:
    """In-flight tool calls per org.

    In process only. With several workers each holds its own view, so the
    effective limit is `limit × workers` — stated plainly here and in the docs
    rather than discovered later. A shared Redis counter is the fix, and is
    recorded as a known limitation until one exists.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def current(self, org_id: uuid.UUID) -> int:
        with self._lock:
            return self._counts.get(str(org_id), 0)

    def acquire(self, org_id: uuid.UUID) -> None:
        key = str(org_id)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def release(self, org_id: uuid.UUID) -> None:
        key = str(org_id)
        with self._lock:
            remaining = self._counts.get(key, 0) - 1
            if remaining > 0:
                self._counts[key] = remaining
            else:
                self._counts.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


concurrency = _ConcurrencyTracker()


@contextmanager
def in_flight(org_id: uuid.UUID):
    """Count one call as in flight for the duration of the block."""
    concurrency.acquire(org_id)
    try:
        yield
    finally:
        concurrency.release(org_id)


# ── Evaluation ───────────────────────────────────────────────────────────────


def _applicable(quota: Quota, integration_id: str, tool_name: str) -> bool:
    if quota.scope == SCOPE_TENANT:
        return True
    if quota.scope == SCOPE_INTEGRATION:
        return quota.scope_id == integration_id
    if quota.scope == SCOPE_TOOL:
        return quota.scope_id == f"{integration_id}/{tool_name}"
    return False


def _used(
    session: Session, quota: Quota, integration_id: str, tool_name: str, now: datetime
) -> int:
    """How much of this quota's metric has been consumed in the current window."""
    since = _window_start(quota.kind, now)
    statement = (
        select(func.coalesce(func.sum(UsageEvent.quantity), 0))
        .where(UsageEvent.org_id == quota.org_id)
        .where(UsageEvent.kind == KIND_TOOL_CALL)
        .where(UsageEvent.timestamp >= since)
    )
    if quota.scope == SCOPE_INTEGRATION:
        statement = statement.where(UsageEvent.integration_id == quota.scope_id)
    elif quota.scope == SCOPE_TOOL:
        statement = statement.where(UsageEvent.integration_id == integration_id).where(
            UsageEvent.tool_name == tool_name
        )
    if quota.kind in _TOKEN_KINDS or quota.kind in _TRANSFER_KINDS:
        # Tokens and bytes are not recorded per call yet; a quota on them
        # cannot be enforced without inventing a number, so it is reported as
        # unenforceable rather than silently passing or silently blocking.
        return -1
    return int(session.exec(statement).one())


def evaluate(
    session: Session, org_id: uuid.UUID, integration_id: str, tool_name: str
) -> QuotaVerdict:
    """Check every quota that applies to this call. First breach wins.

    Returns ALLOWED when no quota is configured, which is every install that
    has not opted in.
    """
    quotas = list(
        session.exec(select(Quota).where(Quota.org_id == org_id).where(Quota.enabled)).all()
    )
    if not quotas:
        return ALLOWED

    now = _now()
    for quota in quotas:
        if not _applicable(quota, integration_id, tool_name):
            continue
        if quota.limit_value <= 0:
            continue

        if quota.kind == CONCURRENT_TOOL_CALLS:
            in_flight_now = concurrency.current(org_id)
            if in_flight_now >= quota.limit_value:
                return QuotaVerdict(
                    allowed=False,
                    kind=quota.kind,
                    scope=quota.scope,
                    limit=quota.limit_value,
                    used=in_flight_now,
                    retry_after=1,
                )
            continue

        used = _used(session, quota, integration_id, tool_name, now)
        if used < 0:
            logger.warning(
                "quota %s on org %s cannot be enforced: the metric is not recorded per call",
                quota.kind,
                org_id,
                extra={"tenant_id": str(org_id)},
            )
            continue
        if quota.kind in _CALL_KINDS and used >= quota.limit_value:
            return QuotaVerdict(
                allowed=False,
                kind=quota.kind,
                scope=quota.scope,
                limit=quota.limit_value,
                used=used,
                retry_after=_seconds_until_window_rolls(quota.kind, now),
            )
    return ALLOWED
