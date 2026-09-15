"""The lease that decides which replica runs the work that must run once.

The bug this prevents is quiet: the deployment sweep meters runtime minutes, so
two replicas sweeping the same five minutes bill a tenant for ten. Nothing
raises and nothing logs — the number is simply wrong. So the tests here are
mostly about contention: two owners, one lease, and which one loses.
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from sutr.models.leader_lease import LeaderLease
from sutr.platform import leadership

ALICE = "alice:1:aaaa"
BOB = "bob:2:bbbb"
JOB = leadership.JOB_MAINTENANCE


@pytest.fixture(autouse=True)
def clean_held_state():
    """`_held` is process state; a test must not inherit another's."""
    leadership._held.clear()
    yield
    leadership._held.clear()


def _lease(session) -> LeaderLease:
    return session.exec(select(LeaderLease).where(LeaderLease.job == JOB)).one()


# ── Taking, renewing, losing ─────────────────────────────────────────────────


def test_the_first_replica_to_ask_gets_it(session):
    assert leadership.acquire(session, JOB, owner=ALICE) is True
    assert _lease(session).owner == ALICE
    assert _lease(session).generation == 1


def test_a_second_replica_is_refused_while_the_lease_is_live(session):
    leadership.acquire(session, JOB, owner=ALICE)

    assert leadership.acquire(session, JOB, owner=BOB) is False
    assert _lease(session).owner == ALICE


def test_renewing_pushes_the_expiry_forward_without_changing_hands(session):
    start = datetime(2026, 9, 1, 12, 0, 0)
    leadership.acquire(session, JOB, owner=ALICE, now=start)
    first_expiry = _lease(session).expires_at

    leadership.acquire(session, JOB, owner=ALICE, now=start + timedelta(seconds=15))

    lease = _lease(session)
    assert lease.owner == ALICE
    assert lease.expires_at > first_expiry
    # A renewal is not a takeover, and the generation is how an operator tells
    # a steady leader from one that is flapping.
    assert lease.generation == 1


def test_an_expired_lease_is_taken_over(session):
    start = datetime(2026, 9, 1, 12, 0, 0)
    leadership.acquire(session, JOB, owner=ALICE, now=start)

    later = start + timedelta(seconds=leadership.LEASE_SECONDS + 1)
    assert leadership.acquire(session, JOB, owner=BOB, now=later) is True

    lease = _lease(session)
    assert lease.owner == BOB
    assert lease.generation == 2
    assert lease.acquired_at == later


def test_only_one_of_two_replicas_takes_an_expired_lease(session):
    """The race that matters: both see it expired, one must lose."""
    start = datetime(2026, 9, 1, 12, 0, 0)
    leadership.acquire(session, JOB, owner=ALICE, now=start)
    later = start + timedelta(seconds=leadership.LEASE_SECONDS + 1)

    first = leadership.acquire(session, JOB, owner=BOB, now=later)
    second = leadership.acquire(session, JOB, owner="carol:3:cccc", now=later)

    assert (first, second) == (True, False)
    assert _lease(session).owner == BOB
    # One takeover, not two.
    assert _lease(session).generation == 2


def test_releasing_hands_it_over_immediately(session):
    leadership.acquire(session, JOB, owner=ALICE)

    assert leadership.release(session, JOB, owner=ALICE) is True
    assert session.exec(select(LeaderLease).where(LeaderLease.job == JOB)).first() is None
    assert leadership.acquire(session, JOB, owner=BOB) is True


def test_a_replica_cannot_release_a_lease_it_does_not_hold(session):
    leadership.acquire(session, JOB, owner=ALICE)

    assert leadership.release(session, JOB, owner=BOB) is False
    assert _lease(session).owner == ALICE


def test_each_job_is_leased_separately(session):
    """So a standalone relay worker and an API replica can each hold one."""
    assert leadership.acquire(session, leadership.JOB_EVENT_RELAY, owner=ALICE) is True
    assert leadership.acquire(session, leadership.JOB_MAINTENANCE, owner=BOB) is True

    assert leadership.holder(session, leadership.JOB_EVENT_RELAY) == ALICE
    assert leadership.holder(session, leadership.JOB_MAINTENANCE) == BOB


def test_an_expired_lease_has_no_holder(session):
    start = datetime(2026, 9, 1, 12, 0, 0)
    leadership.acquire(session, JOB, owner=ALICE, now=start)

    past_expiry = start + timedelta(seconds=leadership.LEASE_SECONDS + 1)
    assert leadership.holder(session, JOB, now=past_expiry) is None
    assert leadership.holder(session, JOB, now=start + timedelta(seconds=1)) == ALICE


def test_the_lease_outlives_several_renewals(session):
    """A single slow database call must not hand the job to another replica."""
    assert leadership.LEASE_SECONDS > 2 * leadership.RENEW_SECONDS


def test_what_this_process_holds_is_reported(session):
    leadership.acquire(session, JOB, owner=leadership.instance_id())

    described = leadership.describe()
    assert JOB in described["held"]
    assert set(described["jobs"]) == set(leadership.SINGLETON_JOBS)
    assert described["instance_id"] == leadership.instance_id()


def test_losing_a_lease_clears_what_this_process_believes_it_holds(session):
    start = datetime(2026, 9, 1, 12, 0, 0)
    me = leadership.instance_id()
    leadership.acquire(session, JOB, owner=me, now=start)
    assert JOB in leadership.held_jobs()

    later = start + timedelta(seconds=leadership.LEASE_SECONDS + 1)
    leadership.acquire(session, JOB, owner=BOB, now=later)
    leadership.acquire(session, JOB, owner=me, now=later + timedelta(seconds=1))

    assert JOB not in leadership.held_jobs()


def test_the_instance_id_is_more_than_host_and_pid():
    """Two containers can share a hostname and pids repeat."""
    generated = leadership.instance_id()
    assert generated.count(":") == 2
    assert leadership.instance_id() == generated  # stable within a process


# ── The supervisor ───────────────────────────────────────────────────────────


async def test_the_work_runs_only_while_the_lease_is_held(session, monkeypatch):
    running = asyncio.Event()
    cancelled = asyncio.Event()
    leader = True

    async def work():
        running.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def fake_acquire(job):
        return leader

    monkeypatch.setattr(leadership, "_acquire_in_session", fake_acquire)
    monkeypatch.setattr(leadership, "_holder_in_session", lambda job: BOB)
    monkeypatch.setattr(leadership, "_release_in_session", lambda job: None)

    supervisor = asyncio.create_task(leadership.run_as_singleton(JOB, work, renew_seconds=0.01))
    await asyncio.wait_for(running.wait(), timeout=2)

    # The lease goes to someone else; the work must stop here.
    leader = False
    await asyncio.wait_for(cancelled.wait(), timeout=2)

    supervisor.cancel()
    try:
        await supervisor
    except asyncio.CancelledError:
        pass


async def test_a_replica_that_never_wins_never_starts_the_work(session, monkeypatch):
    started = False

    async def work():
        nonlocal started
        started = True
        await asyncio.sleep(3600)

    monkeypatch.setattr(leadership, "_acquire_in_session", lambda job: False)
    monkeypatch.setattr(leadership, "_release_in_session", lambda job: None)

    supervisor = asyncio.create_task(leadership.run_as_singleton(JOB, work, renew_seconds=0.01))
    await asyncio.sleep(0.1)
    supervisor.cancel()
    try:
        await supervisor
    except asyncio.CancelledError:
        pass

    assert started is False


async def test_a_database_that_cannot_be_reached_stops_the_work(session, monkeypatch):
    """The safe direction: the work writes to the database it cannot reach."""
    cancelled = asyncio.Event()
    reachable = True

    async def work():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    def fake_acquire(job):
        if not reachable:
            raise RuntimeError("database is gone")
        return True

    monkeypatch.setattr(leadership, "_acquire_in_session", fake_acquire)
    monkeypatch.setattr(leadership, "_holder_in_session", lambda job: None)
    monkeypatch.setattr(leadership, "_release_in_session", lambda job: None)

    supervisor = asyncio.create_task(leadership.run_as_singleton(JOB, work, renew_seconds=0.01))
    await asyncio.sleep(0.1)
    reachable = False
    await asyncio.wait_for(cancelled.wait(), timeout=2)

    supervisor.cancel()
    try:
        await supervisor
    except asyncio.CancelledError:
        pass


async def test_shutting_down_hands_the_lease_back(session, monkeypatch):
    released = []

    async def work():
        await asyncio.sleep(3600)

    def fake_acquire(job):
        leadership._held.add(job)
        return True

    monkeypatch.setattr(leadership, "_acquire_in_session", fake_acquire)
    monkeypatch.setattr(leadership, "_release_in_session", released.append)

    supervisor = asyncio.create_task(leadership.run_as_singleton(JOB, work, renew_seconds=0.01))
    await asyncio.sleep(0.05)
    supervisor.cancel()
    try:
        await supervisor
    except asyncio.CancelledError:
        pass

    # Best effort, and the point of it: a peer takes over in seconds rather
    # than waiting out the lease.
    assert released == [JOB]


# ── The jobs themselves ──────────────────────────────────────────────────────


def test_every_leased_job_is_one_that_writes():
    """A read-only loop under a lease would be a pointless single point of work."""
    assert set(leadership.SINGLETON_JOBS) == {
        "maintenance",
        "deployment_monitor",
        "source_sync",
        "event_relay",
    }


def test_the_deployment_sweep_is_leased_because_it_meters(session):
    """Named explicitly: this is the job whose double-run costs a tenant money."""
    import inspect

    from sutr import maintenance

    assert "record_deployment_runtime" in inspect.getsource(maintenance.sweep_deployments)
    assert leadership.JOB_DEPLOYMENT_MONITOR in leadership.SINGLETON_JOBS


async def test_a_supervisor_that_stopped_its_work_can_still_be_shut_down(session, monkeypatch):
    """Found live, and the reason for `asyncio.wait` in `_cancel`.

    Awaiting a cancelled task re-raises its CancelledError in the supervisor.
    Swallowing that left the supervisor unable to be cancelled itself: it
    stopped renewing, logged nothing, and hung the process on shutdown — a
    leader that had quietly stopped leading while looking perfectly healthy.
    """
    state = {"leader": True}

    async def work():
        await asyncio.sleep(3600)

    monkeypatch.setattr(leadership, "_acquire_in_session", lambda job: state["leader"])
    monkeypatch.setattr(leadership, "_holder_in_session", lambda job: BOB)
    monkeypatch.setattr(leadership, "_release_in_session", lambda job: None)

    supervisor = asyncio.create_task(leadership.run_as_singleton(JOB, work, renew_seconds=0.01))
    await asyncio.sleep(0.05)
    # Lose the lease, so the supervisor cancels the work it started.
    state["leader"] = False
    await asyncio.sleep(0.1)

    supervisor.cancel()
    # The assertion is simply that this returns at all.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(supervisor, timeout=2)
