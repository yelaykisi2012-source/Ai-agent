"""Database layer tests."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

import database
from config import PROJECT_ROOT, Settings
from database import EventLog, build_dsn, dispose_database, init_database, record_event, session_scope


def test_relative_sqlite_path_resolves_against_project_root() -> None:
    dsn = build_dsn("sqlite:///./data/agent.db")
    assert isinstance(dsn.database, str)
    resolved = Path(dsn.database)
    assert resolved.is_absolute()
    assert resolved == PROJECT_ROOT / "data" / "agent.db"


def test_absolute_sqlite_path_is_preserved(tmp_path) -> None:
    target = tmp_path / "nested" / "custom.db"
    dsn = build_dsn(f"sqlite:///{target.as_posix()}")
    assert Path(dsn.database) == target
    assert target.parent.is_dir()  # parent directory is created


def test_memory_database_is_untouched() -> None:
    assert build_dsn("sqlite://").database in (None, "")


def test_event_history_round_trip(settings: Settings) -> None:
    init_database(settings)
    record_event(source="test", message="hello history")
    record_event(source="test", message="failure", level="ERROR", details='{"reason": "x"}')

    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(EventLog))
        latest = session.scalars(select(EventLog).order_by(EventLog.id.desc())).first()

    assert total == 2
    assert latest is not None
    assert latest.level == "ERROR"
    assert latest.details == '{"reason": "x"}'
    dispose_database()


def test_session_scope_rolls_back_on_error(settings: Settings) -> None:
    init_database(settings)
    try:
        with session_scope() as session:
            session.add(EventLog(source="test", message="will be rolled back"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(EventLog))
    assert total == 0
    dispose_database()
