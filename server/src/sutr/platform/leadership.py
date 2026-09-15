"""Exactly one replica runs the work that must happen once.

Running the API twice is the point of horizontal scaling. Running the
*background loops* twice is a bug, and a quiet one: the deployment sweep meters
runtime minutes, so two replicas sweeping the same five minutes bill a tenant
for ten. Nothing fails, nothing logs an error, and the number is simply wrong.

So every loop that must be a singleton runs under a lease
(`models/leader_lease.py`). A replica takes the lease for a job, renews it
while it works, and stops renewing when it dies; another replica takes over
once the lease expires. There is no coordinator, no broker and no new
dependency — the database both planes already share is enough.

Two properties are deliberate:

- **Losing the lease stops the work.** The renewal check cancels the loop it
  supervises rather than logging and continuing. A replica that has lost
  leadership and keeps sweeping is the exact scenario the lease exists to
  prevent.
- **Overlap is possible and bounded.** A leader that stalls for longer than the
  lease may still think it is leader while another takes over. Every job under
  a lease is therefore idempotent or sampling: an overlap over-counts one
  sample rather than corrupting a total.
"""

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from sutr import db
from sutr.models.leader_lease import LeaderLease

logger = logging.getLogger(__name__)

# The jobs that must run exactly once across the deployment. Named here rather
# than at each call site so the list can be reported, tested, and read by an
# operator asking what leadership is even for.
JOB_MAINTENANCE = "maintenance"
JOB_DEPLOYMENT_MONITOR = "deployment_monitor"
JOB_SOURCE_SYNC = "source_sync"
JOB_EVENT_RELAY = "event_relay"

SINGLETON_JOBS = (
    JOB_MAINTENANCE,
    JOB_DEPLOYMENT_MONITOR,
    JOB_SOURCE_SYNC,
    JOB_EVENT_RELAY,
)

# A lease outlives several renewals, so one slow database call does not hand
# the job to another replica; renewal is frequent enough that a crashed leader
# is replaced in well under a minute.
LEASE_SECONDS = 45
RENEW_SECONDS = 15

_instance_id: str | None = None
# What this process currently holds. Read by the readiness endpoint, which is
# how an operator finds the sweeping replica without querying the database.
_held: set[str] = set()


def instance_id() -> str:
    """A stable identifier for this process.

    Host and pid would be enough in practice and are not enough in principle:
    two containers can share a hostname, and pids repeat. The random suffix
    costs nothing and removes the question.
    """
    global _instance_id
    if _instance_id is None:
        _instance_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    return _instance_id


def held_jobs() -> tuple[str, ...]:
    return tuple(sorted(_held))


def acquire(session: Session, job: str, *, owner: str | None = None, now=None) -> bool:
    """Take or renew the lease for `job`. True when this process now holds it.

    Three statements, in this order, because each one is a single atomic write
    and the ordering is what makes the whole thing safe without a transaction
    the caller has to get right:

    1. **Renew** the row if it is already ours.
    2. **Take over** the row if its lease has expired. Two replicas racing here
       both run the same conditional UPDATE, and only one matches: the winner
       moves `expires_at` forward, so the loser's `WHERE expires_at <= now` no
       longer selects anything and it correctly reports that it is not leader.
    3. **Insert** if there is no row at all. A duplicate insert collides on the
       primary key, which is the same race resolved the same way.
    """
    owner = owner or instance_id()
    now = now or datetime.utcnow()
    expires = now + timedelta(seconds=LEASE_SECONDS)
    table = LeaderLease.__table__

    renewed = session.execute(
        update(table)
        .where(table.c.job == job)
        .where(table.c.owner == owner)
        .values(renewed_at=now, expires_at=expires)
    )
    if renewed.rowcount == 1:
        session.commit()
        _held.add(job)
        return True

    taken = session.execute(
        update(table)
        .where(table.c.job == job)
        .where(table.c.expires_at <= now)
        .values(
            owner=owner,
            acquired_at=now,
            renewed_at=now,
            expires_at=expires,
            generation=table.c.generation + 1,
        )
    )
    if taken.rowcount == 1:
        session.commit()
        _held.add(job)
        logger.info("took leadership of %s as %s", job, owner)
        return True

    session.rollback()
    if session.exec(select(LeaderLease).where(LeaderLease.job == job)).first() is not None:
        # Somebody else holds a live lease. Nothing to do but wait.
        _held.discard(job)
        return False

    try:
        session.add(
            LeaderLease(
                job=job,
                owner=owner,
                acquired_at=now,
                renewed_at=now,
                expires_at=expires,
            )
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        _held.discard(job)
        return False

    _held.add(job)
    logger.info("took leadership of %s as %s", job, owner)
    return True


def release(session: Session, job: str, *, owner: str | None = None) -> bool:
    """Give up the lease, so a peer takes over immediately rather than in a minute.

    Best effort by design: a replica that is being killed may not get here, and
    the lease expiring is the path that has to work anyway.
    """
    owner = owner or instance_id()
    row = session.exec(select(LeaderLease).where(LeaderLease.job == job)).first()
    _held.discard(job)
    if row is None or row.owner != owner:
        return False
    session.delete(row)
    session.commit()
    logger.info("released leadership of %s", job)
    return True


def holder(session: Session, job: str, *, now=None) -> str | None:
    """Who holds a live lease for `job`, or None. An expired lease has no holder."""
    now = now or datetime.utcnow()
    row = session.exec(select(LeaderLease).where(LeaderLease.job == job)).first()
    if row is None or row.expires_at <= now:
        return None
    return row.owner


def describe() -> dict:
    """What leadership is doing in this process, for the readiness report."""
    return {
        "instance_id": instance_id(),
        "jobs": list(SINGLETON_JOBS),
        "held": list(held_jobs()),
        "lease_seconds": LEASE_SECONDS,
        "renew_seconds": RENEW_SECONDS,
    }


def _acquire_in_session(job: str) -> bool:
    with Session(db.engine) as session:
        return acquire(session, job)


def _holder_in_session(job: str) -> str | None:
    """Who holds it now. Best effort — this runs while explaining a loss."""
    try:
        with Session(db.engine) as session:
            return holder(session, job)
    except Exception:
        return None


def _release_in_session(job: str) -> None:
    with Session(db.engine) as session:
        release(session, job)


async def run_as_singleton(job: str, factory, *, renew_seconds: float = RENEW_SECONDS) -> None:
    """Run `factory()` on whichever replica holds the lease for `job`.

    Loops forever: waiting for the lease when another replica has it, running
    the work and renewing while it holds it, and stopping the work the moment a
    renewal fails. A database that is down means no renewal, which means the
    work stops — the safe direction, since the work writes to that database.
    """
    task: asyncio.Task | None = None
    # What this process believed a moment ago, so that *every* change of state
    # is logged — including losing a lease at a moment when no task happened to
    # be running. A leadership change that leaves no trace is a change nobody
    # can explain afterwards, which is the worst kind to have in a system whose
    # whole job is deciding who does the work.
    believed_leader = False
    try:
        while True:
            try:
                is_leader = await asyncio.to_thread(_acquire_in_session, job)
            except Exception:
                logger.exception("could not reach the database to renew leadership of %s", job)
                is_leader = False

            if believed_leader and not is_leader:
                holder_now = await asyncio.to_thread(_holder_in_session, job)
                logger.warning(
                    "lost leadership of %s (holder is now %s); stopping it here",
                    job,
                    holder_now or "nobody",
                )
            believed_leader = is_leader

            if is_leader and task is None:
                task = asyncio.create_task(factory(), name=f"sutr-{job}")
            elif not is_leader and task is not None:
                await _cancel(task)
                task = None

            if task is not None and task.done():
                # The loop returned or raised on its own. Surface it and let
                # the next pass start it again while we still hold the lease.
                exception = task.exception() if not task.cancelled() else None
                if exception is not None:
                    logger.exception("%s stopped unexpectedly", job, exc_info=exception)
                task = None

            await asyncio.sleep(renew_seconds)
    except asyncio.CancelledError:
        raise
    finally:
        if task is not None:
            await _cancel(task)
        if job in _held:
            try:
                await asyncio.to_thread(_release_in_session, job)
            except Exception:
                logger.warning("could not release leadership of %s on shutdown", job)


async def _cancel(task: asyncio.Task) -> None:
    """Stop a supervised loop and wait for it to actually be gone.

    `asyncio.wait` rather than `await task`: awaiting a cancelled task re-raises
    its CancelledError here, and swallowing that leaves this coroutine's own
    cancellation state confused — a supervisor that then refuses to stop on
    shutdown. Waiting observes the outcome without adopting it.
    """
    task.cancel()
    await asyncio.wait({task})
