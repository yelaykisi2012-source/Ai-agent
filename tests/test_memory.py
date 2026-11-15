"""Memory layer tests."""

from __future__ import annotations

import memory
from config import Settings
from database import init_database, session_scope
from models import Message


def _init(settings: Settings) -> None:
    init_database(settings)


def test_title_is_single_line_and_truncated() -> None:
    assert memory.make_title("  hello\n\nworld  ") == "hello world"
    assert memory.make_title("") == "New conversation"
    long_title = memory.make_title("x" * 200)
    assert len(long_title) <= memory.TITLE_MAX_LENGTH
    assert long_title.endswith("\u2026")


def test_conversation_round_trip(settings: Settings) -> None:
    _init(settings)
    conversation = memory.create_conversation("Birinchi savol")
    assert conversation.id >= 1
    assert conversation.title == "Birinchi savol"

    memory.add_message(conversation.id, "user", "Salom")
    memory.add_message(conversation.id, "assistant", "Assalomu alaykum")

    transcript = memory.get_transcript(conversation.id)
    assert transcript is not None
    _, messages = transcript
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "Assalomu alaykum"


def test_context_limit_returns_chronological_window(settings: Settings) -> None:
    _init(settings)
    conversation = memory.create_conversation("many")
    for index in range(30):
        memory.add_message(conversation.id, "user", f"message {index}")

    recent = memory.recent_messages(conversation.id, 5)
    assert [m.content for m in recent] == [f"message {i}" for i in range(25, 30)]


def test_invalid_role_is_rejected(settings: Settings) -> None:
    _init(settings)
    conversation = memory.create_conversation("roles")
    try:
        memory.add_message(conversation.id, "hacker", "nope")
    except ValueError as exc:
        assert "invalid message role" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("invalid role was accepted")


def test_delete_conversation_removes_messages(settings: Settings) -> None:
    _init(settings)
    conversation = memory.create_conversation("delete me")
    memory.add_message(conversation.id, "user", "hi")

    assert memory.delete_conversation(conversation.id) is True
    assert memory.delete_conversation(conversation.id) is False
    with session_scope() as session:
        remaining = session.query(Message).filter_by(conversation_id=conversation.id).count()
    assert remaining == 0


def test_list_conversations_reports_counts(settings: Settings) -> None:
    _init(settings)
    first = memory.create_conversation("first")
    memory.add_message(first.id, "user", "a")
    memory.add_message(first.id, "assistant", "b")
    memory.create_conversation("second")

    stats, total = memory.list_conversations(limit=10, offset=0)
    assert total == 2
    assert {item.title for item in stats} == {"first", "second"}
    first_stats = next(item for item in stats if item.id == first.id)
    assert first_stats.message_count == 2
    assert first_stats.last_message_at is not None


def test_long_term_memory_lifecycle(settings: Settings) -> None:
    _init(settings)
    entry = memory.add_memory("I prefer morning reminders", category="preference")
    assert entry.id >= 1

    found = memory.search_memories("morning")
    assert [item.id for item in found] == [entry.id]
    assert memory.search_memories("nothing-matches-this") == []

    updated = memory.update_memory(entry.id, content="I prefer 8am reminders")
    assert updated is not None
    assert updated.content == "I prefer 8am reminders"

    stats = memory.memory_stats()
    assert stats["total"] == 1
    assert stats["preference"] == 1

    assert memory.delete_memory(entry.id) is True
    assert memory.delete_memory(entry.id) is False
    assert memory.memory_stats().get("total", 0) == 0


def test_pending_action_is_single_use(settings: Settings) -> None:
    _init(settings)
    action_id = memory.create_pending_action("initiate_telegram_call", {"reason": "x"}, "summary")

    action = memory.get_pending_action(action_id)
    assert action is not None
    assert action.tool == "initiate_telegram_call"
    assert memory.latest_pending_action().id == action_id  # type: ignore[union-attr]

    assert memory.consume_pending_action(action_id) is True
    assert memory.consume_pending_action(action_id) is False
    assert memory.get_pending_action(action_id) is None


def test_pause_state_round_trip(settings: Settings) -> None:
    _init(settings)
    assert memory.is_paused() is False
    memory.set_paused(True)
    assert memory.is_paused() is True
    memory.set_paused(False)
    assert memory.is_paused() is False
