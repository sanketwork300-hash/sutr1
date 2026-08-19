from collections.abc import Generator

from sqlmodel import Session, create_engine

from agent_port.config import settings

connect_args = {}
engine_kwargs = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
else:
    # Recycle dead pooled connections (e.g. after a Postgres restart or
    # load-balancer idle timeout) instead of failing the first request.
    engine_kwargs["pool_pre_ping"] = True

engine = create_engine(settings.database_url, connect_args=connect_args, **engine_kwargs)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
