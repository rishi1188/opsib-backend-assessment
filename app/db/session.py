"""Engine/session factory and the per-tenant write lock."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def acquire_tenant_lock(session: Session, tenant_id: str, scope: str) -> None:
    """Serialise writers for one tenant and scope inside the current transaction.

    On PostgreSQL this is a transaction-scoped advisory lock, released on
    commit or rollback, so two workers importing or detecting for the same
    tenant run one after the other. SQLite already allows only one writer at a
    time, so no extra lock is needed there. Unique constraints remain the last
    line of defence either way.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{tenant_id}:{scope}"},
        )
