"""Database engine / session setup and schema bootstrap."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dealfinder.config import get_settings
from dealfinder.core.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _ensure_sqlite_dir(url: str) -> None:
    """Make sure the parent directory exists for a file-based SQLite URL."""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        db_path = Path(url[len(prefix):])
        if db_path.parent and not db_path.parent.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        _ensure_sqlite_dir(url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            _enable_sqlite_wal(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _enable_sqlite_wal(engine: Engine) -> None:
    """WAL + a busy timeout so the web and worker processes don't trip 'database is
    locked' when they write the same SQLite file concurrently (finding O1)."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


def init_db() -> None:
    """Create all tables. For MVP this stands in for ``alembic upgrade head``."""
    get_engine()
    Base.metadata.create_all(bind=get_engine())


def _session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager (commit on success, rollback on error)."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session (read-mostly; caller commits if needed)."""
    session = _session_factory()()
    try:
        yield session
    finally:
        session.close()
