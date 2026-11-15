"""Database layer: engine, sessions and the execution-history table.

SQLite is used for Phase 1. The engine is created once at startup and shared;
sessions are short-lived and always closed. SQLite is configured with WAL mode
and a busy timeout so the future background scheduler and web requests can
write without tripping over each other.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, create_engine, event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import PROJECT_ROOT, Settings, get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


class EventLog(Base):
    """Append-only execution history.

    Every meaningful agent action (startup, tool execution, reminder fired,
    call placed, failure) is recorded here so the user can audit what the
    agent did and why.
    """

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    level: Mapped[str] = mapped_column(String(16), default="INFO", nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="agent", nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EventLog(id={self.id!r}, source={self.source!r}, level={self.level!r})"


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_dsn(database_url: str) -> URL | str:
    """Normalise a database URL.

    Relative SQLite file paths are resolved against the project root so the
    agent uses the same database no matter which directory it is started from.
    The parent directory is created if missing. Non-SQLite URLs pass through
    unchanged, so moving to PostgreSQL later means changing only ``.env``.
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return url

    database = url.database
    if not database or database in {":memory:", ""}:
        return url

    path = Path(database)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return URL.create("sqlite", database=str(path))


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Apply durability/concurrency pragmas to every new SQLite connection."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


def init_database(settings: Settings | None = None) -> Engine:
    """Create the engine (once) and ensure the schema exists."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    backend = make_url(settings.database_url).get_backend_name()
    engine_kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if backend == "sqlite":
        engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

    engine = create_engine(build_dsn(settings.database_url), **engine_kwargs)
    if backend == "sqlite":
        _install_sqlite_pragmas(engine)

    # Import ORM modules so their tables are registered on Base.metadata.
    # Local import: models.py imports Base from this module, so a top-level
    # import would be circular.
    import models  # noqa: F401

    Base.metadata.create_all(engine)
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    logger.info("Database ready (%s backend)", backend)
    return engine


def get_engine() -> Engine:
    """Return the initialised engine or fail loudly."""
    if _engine is None:
        raise RuntimeError("Database is not initialised; call init_database() first")
    return _engine


def _require_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("Database is not initialised; call init_database() first")
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commit on success, roll back on error, always close."""
    session = _require_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_event(
    *,
    source: str,
    message: str,
    level: str = "INFO",
    details: str | None = None,
) -> None:
    """Append an entry to the execution history.

    History writes must never take down the agent: a failure is logged to the
    Python logger and swallowed.
    """
    try:
        with session_scope() as session:
            session.add(
                EventLog(source=source, message=message, level=level, details=details)
            )
    except Exception:  # pragma: no cover - defensive path
        logger.exception("Failed to write execution history event: %s", message)


def dispose_database() -> None:
    """Close pooled connections and reset module state (used on shutdown/tests)."""
    global _engine, _session_factory

    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
