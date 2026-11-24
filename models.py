"""ORM models for KSENUS AI.

Phase 1 keeps the event log in ``database.py``. Everything user-facing lives
here: conversations, tasks, reminders, sleep checks, long-term memory, call
history, pending confirmations and runtime state.

All timestamps are stored in UTC. SQLite returns them without an offset, so
callers that need local time convert explicitly via ``config.Settings.tzinfo``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

VALID_ROLES: frozenset[str] = frozenset({"user", "assistant", "system", "tool"})
VALID_TASK_STATUSES: frozenset[str] = frozenset({"pending", "done", "cancelled"})
VALID_PRIORITIES: frozenset[str] = frozenset({"low", "normal", "high"})
VALID_REMINDER_STATUSES: frozenset[str] = frozenset(
    {"pending", "sent", "acknowledged", "missed", "cancelled"}
)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used as a column default."""
    return datetime.now(timezone.utc)


class Conversation(Base):
    """A single thread of messages with the user."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
        passive_deletes=True,
    )


class Message(Base):
    """One turn in a conversation, stored verbatim."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Task(Base):
    """A user task with an optional deadline and priority."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), default="pending", nullable=False, index=True
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Reminder(Base):
    """A scheduled notification owned by the scheduler.

    ``kind`` distinguishes a normal reminder, a task deadline and a sleep
    check. Retries are tracked with ``attempts`` so the agent can report an
    honest "no response" instead of pretending the user was reached.
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    sleep_check_id: Mapped[int | None] = mapped_column(
        ForeignKey("sleep_checks.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), default="reminder", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    #: "" (one-off), "daily" or "weekly". A repeating reminder is re-created
    #: for its next occurrence as soon as this one is acknowledged.
    repeat: Mapped[str] = mapped_column(
        String(16), default="", server_default="", nullable=False
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Reminder(id={self.id!r}, kind={self.kind!r}, status={self.status!r})"


class SleepCheck(Base):
    """A sleep check with an honest outcome.

    ``status`` is one of active, completed, no_response, cancelled. There is
    deliberately no "asleep" value: absence of a reply is not evidence of
    sleep, so the agent never claims to know it.
    """

    __tablename__ = "sleep_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expected_wake_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False, index=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemoryEntry(Base):
    """A durable fact, preference or instruction the user wants remembered."""

    __tablename__ = "memory_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(
        String(32), default="general", nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CallRecord(Base):
    """Telegram call history. Never contains call content or session data."""

    __tablename__ = "call_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    peer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PendingAction(Base):
    """A sensitive tool call waiting for explicit user confirmation."""

    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MessengerPin(Base):
    """The owner's permanent 4-digit code for the mini messenger.

    Only one row is ever written. The code itself is never stored: the row
    keeps a PBKDF2 hash plus its salt, so reading the database does not reveal
    the PIN, and there is deliberately no endpoint that changes it.
    """

    __tablename__ = "messenger_pin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pin_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    salt: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class RuntimeState(Base):
    """Small key/value store for runtime flags (paused state, owner id)."""

    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AgentRun(Base):
    """One autonomous turn: what triggered it and what it actually did.

    Every channel (HTTP, Telegram, phone call, reminder, desktop) records a
    row, so ``/runs`` can answer "what did the agent do on its own, and why"
    without reading logs.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(
        String(24), default="chat", nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(16), default="http", nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), default="running", nullable=False, index=True
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    reply: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tools: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AgentRun(id={self.id!r}, trigger={self.trigger!r}, status={self.status!r})"
