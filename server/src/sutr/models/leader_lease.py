import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class LeaderLease(SQLModel, table=True):
    """Which replica is currently running a job that must run exactly once.

    Some background work must not run twice. The deployment sweep meters
    runtime minutes, so two replicas running it bill a tenant twice for the
    same five minutes — a correctness bug that only appears once the platform
    is scaled out, which is exactly when nobody is looking for it.

    A lease rather than a database lock: a lease works the same on SQLite and
    Postgres, survives a connection pooler in transaction mode (a session-level
    advisory lock does not), and is visible — `SELECT * FROM leader_lease`
    answers "which replica is sweeping?" without attaching a debugger.

    The trade is the usual one: a leader that stalls for longer than the lease
    may still believe it holds it while another replica takes over. Every job
    under a lease is therefore either idempotent or sampling — a brief overlap
    over-counts one sample rather than corrupting a total.
    """

    __tablename__ = "leader_lease"

    # One row per job, so the primary key is the job name itself: two rows for
    # one job is the state this table exists to make impossible.
    job: str = Field(primary_key=True)
    # Who holds it: host, process, and a random suffix so two containers with
    # the same hostname and pid cannot be confused for one another.
    owner: str
    acquired_at: datetime = Field(default_factory=datetime.utcnow)
    renewed_at: datetime = Field(default_factory=datetime.utcnow)
    # When another replica may take over. Renewal pushes this forward; a leader
    # that dies stops renewing and the lease expires on its own.
    expires_at: datetime
    # How many times this lease has changed hands, for the operator asking
    # whether leadership is flapping.
    generation: int = Field(default=1)
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
