"""Tool handler tests."""

from __future__ import annotations

import asyncio

from config import Settings
from database import init_database
from fakes import FakeSpeech, FakeTelegram
from runtime import AgentRuntime


def _runtime(settings: Settings) -> AgentRuntime:
    init_database(settings)
    return AgentRuntime(settings, telegram=FakeTelegram(), speech=FakeSpeech())


def test_create_list_and_complete_task_tools(settings: Settings) -> None:
    runtime = _runtime(settings)
    create = asyncio.run(
        runtime.brain.tools.execute(
            "create_task",
            {
                "title": "Prepare presentation",
                "due_at": "2030-01-01T09:00:00+05:00",
                "priority": "high",
            },
        )
    )
    assert create.status == "ok"
    assert "created" in create.detail.lower()

    listed = asyncio.run(runtime.brain.tools.execute("list_tasks", {"status": "pending"}))
    assert "Prepare presentation" in listed.detail

    complete = asyncio.run(runtime.brain.tools.execute("complete_task", {"task_id": 1}))
    assert complete.status == "ok"
    assert "marked done" in complete.detail.lower()


def test_memory_tools_round_trip(settings: Settings) -> None:
    runtime = _runtime(settings)
    saved = asyncio.run(
        runtime.brain.tools.execute(
            "save_memory", {"content": "I prefer evening calls", "category": "preference"}
        )
    )
    assert saved.status == "ok"

    found = asyncio.run(
        runtime.brain.tools.execute("retrieve_memory", {"query": "evening"})
    )
    assert "evening calls" in found.detail

    empty = asyncio.run(
        runtime.brain.tools.execute("retrieve_memory", {"query": "nothing-like-this"})
    )
    assert "no stored memory" in empty.detail.lower()


def test_sleep_check_tool_creates_real_record(settings: Settings) -> None:
    runtime = _runtime(settings)
    result = asyncio.run(
        runtime.brain.tools.execute("start_sleep_check", {"minutes": 20})
    )
    assert result.status == "ok"
    assert "will contact you" in result.detail.lower()
    assert "asleep" in result.detail.lower()  # explicitly refuses to claim it


def test_call_tool_requires_confirmation_then_refuses_honestly(settings: Settings) -> None:
    runtime = _runtime(settings)
    unconfirmed = asyncio.run(
        runtime.brain.tools.execute("initiate_telegram_call", {"reason": "wake up"})
    )
    assert unconfirmed.status == "confirmation_required"
    assert unconfirmed.confirmation_id is not None

    confirmed = asyncio.run(
        runtime.brain.tools.execute(
            "initiate_telegram_call", {"reason": "wake up"}, confirmed=True
        )
    )
    assert confirmed.status == "ok"
    assert "cannot place" in confirmed.detail.lower()


def test_invalid_task_arguments_do_not_crash(settings: Settings) -> None:
    runtime = _runtime(settings)
    result = asyncio.run(runtime.brain.tools.execute("create_task", {"title": " "}))
    assert result.status == "ok"
    assert "need a task title" in result.detail.lower()

    missing = asyncio.run(runtime.brain.tools.execute("complete_task", {}))
    assert "numeric task id" in missing.detail.lower()


def test_schedule_call_stores_a_call_reminder(settings) -> None:
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import asyncio

    from tools import _schedule_call

    created: list[tuple[str, datetime, str]] = []

    class _Reminders:
        def create(self, message, when, *, kind="reminder"):
            created.append((message, when, kind))
            return SimpleNamespace(id=7)

    context = SimpleNamespace(settings=settings, reminders=_Reminders())
    now = datetime.now(settings.tzinfo)

    detail = asyncio.run(_schedule_call(context, {"minutes": 3}))

    assert "Call scheduled" in detail and "#7" in detail
    _message, when, kind = created[0]
    assert kind == "call"
    assert timedelta(minutes=2) < when - now <= timedelta(minutes=4)


def test_schedule_call_refuses_a_past_time(settings) -> None:
    from types import SimpleNamespace
    import asyncio

    from tools import _schedule_call

    created: list[object] = []

    class _Reminders:
        def create(self, *args, **kwargs):
            created.append(args)
            return SimpleNamespace(id=1)

    context = SimpleNamespace(settings=settings, reminders=_Reminders())
    detail = asyncio.run(
        _schedule_call(context, {"at": "2020-01-01T09:00:00+05:00"})
    )

    assert "past" in detail
    assert created == []
