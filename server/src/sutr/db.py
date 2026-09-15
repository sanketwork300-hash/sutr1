"""The engine and the session factory.

Pooling is a single-replica non-issue and a multi-replica correctness one. Each
replica opens its own pool, so the connections PostgreSQL is asked for are
`replicas × (pool_size + max_overflow)`. Exceed `max_connections` and the
failure is not one slow replica — it is every replica failing to connect at
once, including the one that would have served the traffic. The defaults here
are deliberately small for that reason; raise them knowing the multiplication.
"""

from collections.abc import Generator

from sqlmodel import Session, create_engine

from sutr.config import settings


def _engine_options() -> tuple[dict, dict]:
    """(connect_args, engine_kwargs) for the configured database URL."""
    connect_args: dict = {}
    engine_kwargs: dict = {}

    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        return connect_args, engine_kwargs

    # Recycle dead pooled connections (e.g. after a Postgres restart or
    # load-balancer idle timeout) instead of failing the first request.
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_size"] = settings.db_pool_size
    engine_kwargs["max_overflow"] = settings.db_max_overflow
    engine_kwargs["pool_timeout"] = settings.db_pool_timeout_seconds
    # Recycle before a pooler or firewall silently drops a long-idle
    # connection; pre-ping catches the rest.
    engine_kwargs["pool_recycle"] = settings.db_pool_recycle_seconds

    if settings.db_statement_timeout_ms > 0:
        # Enforced by the server, so it also applies to a statement whose
        # client has already gone away — which is the case that pins a
        # connection until someone notices.
        #
        # **Direct connections only.** This travels as a libpq *startup*
        # parameter, and PgBouncer rejects startup parameters it does not know:
        # every replica behind one dies at boot with "unsupported startup
        # parameter in options: statement_timeout". Behind a pooler, set the
        # timeout on the role instead — `ALTER ROLE sutr SET statement_timeout`
        # — which the server applies to every session however it connects. See
        # deploy/ha/postgres/init-replication.sh.
        connect_args["options"] = f"-c statement_timeout={settings.db_statement_timeout_ms}"

    return connect_args, engine_kwargs


_connect_args, _engine_kwargs = _engine_options()

engine = create_engine(settings.database_url, connect_args=_connect_args, **_engine_kwargs)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def pool_status() -> dict:
    """How much of the pool is in use, for the readiness report.

    A pool that is permanently full is the shape of an outage that has not
    happened yet: requests are already queueing for `pool_timeout` seconds
    before anything errors.
    """
    pool = engine.pool
    status = {"dialect": engine.dialect.name}
    for attribute in ("size", "checkedin", "checkedout", "overflow"):
        reader = getattr(pool, attribute, None)
        if callable(reader):
            try:
                status[attribute] = reader()
            except Exception:  # pragma: no cover - pool implementations vary
                pass
    return status
