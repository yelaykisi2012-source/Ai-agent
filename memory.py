"""Persistence for conversations, long-term memory and runtime state.

Every function opens its own short-lived session, so callers never hold a
transaction open across a slow network call to a model provider.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from database import session_scope
from models import (
    VALID_ROLES,
    Conversation,
    MemoryEntry,
    Message,
    PendingAction,
    RuntimeState,
    utcnow,
)

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 80
_TITLE_ELLIPSIS = "\u2026"
PENDING_ACTION_TTL_MINUTES = 10


@dataclass(frozen=True)
class ConversationStats:
    """Lightweight conversation row for list endpoints."""

    id: int
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


# --------------------------------------------------------------------------- #
# Conversations
# --------------------------------------------------------------------------- #
def make_title(text: str, max_length: int = TITLE_MAX_LENGTH) -> str:
    """Build a short, single-line conversation title from the first message."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return "New conversation"
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 1].rstrip() + _TITLE_ELLIPSIS


def create_conversation(first_message: str | None = None) -> Conversation:
    """Insert a conversation and return it (detached, no lazy relations)."""
    now = utcnow()
    with session_scope() as session:
        conversation = Conversation(
            title=make_title(first_message or ""),
            created_at=now,
            updated_at=now,
        )
        session.add(conversation)
        session.flush()
        return conversation


def get_conversation(conversation_id: int) -> Conversation | None:
    """Return a conversation without its messages, or None."""
    with session_scope() as session:
        return session.get(Conversation, conversation_id)


def add_message(conversation_id: int, role: str, content: str) -> Message | None:
    """Append a message and bump the conversation's ``updated_at``."""
    if role not in VALID_ROLES:
        raise ValueError(f"invalid message role: {role!r}")

    now = utcnow()
    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=now,
        )
        session.add(message)
        conversation.updated_at = now
        session.flush()
        return message


def recent_messages(conversation_id: int, limit: int) -> list[Message]:
    """Return up to ``limit`` most recent messages in chronological order."""
    if limit < 1:
        return []
    with session_scope() as session:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
        newest_first = list(session.scalars(stmt))
    newest_first.reverse()
    return newest_first


def all_messages(conversation_id: int) -> list[Message]:
    """Return the full transcript in chronological order."""
    with session_scope() as session:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        )
        return list(session.scalars(stmt))


def get_transcript(conversation_id: int) -> tuple[Conversation, list[Message]] | None:
    """Return the conversation and all of its messages, or None."""
    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id)
        )
        return conversation, list(session.scalars(stmt))


def list_conversations(limit: int, offset: int) -> tuple[list[ConversationStats], int]:
    """Return a page of conversation summaries and the total count."""
    with session_scope() as session:
        stmt = (
            select(
                Conversation,
                func.count(Message.id),
                func.max(Message.created_at),
            )
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .group_by(Conversation.id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = session.execute(stmt).all()
        total = session.scalar(select(func.count()).select_from(Conversation)) or 0

    stats = [
        ConversationStats(
            id=conversation.id,
            title=conversation.title,
            message_count=int(message_count),
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            last_message_at=last_message_at,
        )
        for conversation, message_count, last_message_at in rows
    ]
    return stats, int(total)


def delete_conversation(conversation_id: int) -> bool:
    """Delete a conversation and all of its messages. False if absent."""
    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return False
        session.delete(conversation)
        return True


# --------------------------------------------------------------------------- #
# Long-term memory
# --------------------------------------------------------------------------- #
def add_memory(content: str, category: str = "general", source: str = "chat") -> MemoryEntry:
    """Store one durable memory."""
    cleaned = content.strip()
    if not cleaned:
        raise ValueError("memory content must not be empty")
    now = utcnow()
    with session_scope() as session:
        entry = MemoryEntry(
            content=cleaned,
            category=(category or "general").strip()[:32] or "general",
            source=source,
            created_at=now,
            updated_at=now,
        )
        session.add(entry)
        session.flush()
        return entry


def list_memories(
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
) -> tuple[list[MemoryEntry], int]:
    """Return a page of memories and the total count."""
    with session_scope() as session:
        stmt = select(MemoryEntry)
        count_stmt = select(func.count()).select_from(MemoryEntry)
        if category:
            stmt = stmt.where(MemoryEntry.category == category)
            count_stmt = count_stmt.where(MemoryEntry.category == category)
        stmt = stmt.order_by(MemoryEntry.updated_at.desc(), MemoryEntry.id.desc())
        entries = list(session.scalars(stmt.limit(limit).offset(offset)))
        total = session.scalar(count_stmt) or 0
    return entries, int(total)


def search_memories(query: str, limit: int = 10) -> list[MemoryEntry]:
    """Case-insensitive substring search over stored memories."""
    needle = query.strip()
    if not needle:
        return []
    with session_scope() as session:
        stmt = (
            select(MemoryEntry)
            .where(MemoryEntry.content.ilike(f"%{needle}%"))
            .order_by(MemoryEntry.updated_at.desc(), MemoryEntry.id.desc())
            .limit(limit)
        )
        return list(session.scalars(stmt))


def update_memory(
    memory_id: int,
    *,
    content: str | None = None,
    category: str | None = None,
) -> MemoryEntry | None:
    """Update a memory; returns None when it does not exist."""
    with session_scope() as session:
        entry = session.get(MemoryEntry, memory_id)
        if entry is None:
            return None
        if content is not None:
            cleaned = content.strip()
            if not cleaned:
                raise ValueError("memory content must not be empty")
            entry.content = cleaned
        if category is not None:
            entry.category = category.strip()[:32] or "general"
        entry.updated_at = utcnow()
        session.flush()
        return entry


def delete_memory(memory_id: int) -> bool:
    """Delete one memory. False if absent."""
    with session_scope() as session:
        entry = session.get(MemoryEntry, memory_id)
        if entry is None:
            return False
        session.delete(entry)
        return True


def memory_stats() -> dict[str, int]:
    """Counts per category plus a total, for the dashboard."""
    with session_scope() as session:
        rows = session.execute(
            select(MemoryEntry.category, func.count(MemoryEntry.id)).group_by(
                MemoryEntry.category
            )
        ).all()
    stats = {category: int(count) for category, count in rows}
    stats["total"] = sum(stats.values())
    return stats


# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #
def get_state(key: str, default: str | None = None) -> str | None:
    """Read a runtime flag."""
    with session_scope() as session:
        state = session.get(RuntimeState, key)
        return state.value if state is not None else default


def set_state(key: str, value: str) -> None:
    """Write a runtime flag (upsert)."""
    with session_scope() as session:
        state = session.get(RuntimeState, key)
        if state is None:
            session.add(RuntimeState(key=key, value=value, updated_at=utcnow()))
        else:
            state.value = value
            state.updated_at = utcnow()


def is_paused() -> bool:
    """True when the emergency stop is engaged."""
    return get_state("paused", "false") == "true"


def set_paused(paused: bool) -> None:
    """Engage or release the emergency stop."""
    set_state("paused", "true" if paused else "false")


# --------------------------------------------------------------------------- #
# Pending confirmations
# --------------------------------------------------------------------------- #
def create_pending_action(
    tool: str,
    arguments: dict[str, object],
    summary: str,
    ttl_minutes: int = PENDING_ACTION_TTL_MINUTES,
) -> str:
    """Persist a sensitive tool call and return its confirmation id."""
    action_id = str(uuid.uuid4())
    now = utcnow()
    with session_scope() as session:
        session.add(
            PendingAction(
                id=action_id,
                tool=tool,
                arguments=json.dumps(arguments, ensure_ascii=False),
                summary=summary,
                status="pending",
                created_at=now,
                expires_at=now + timedelta(minutes=ttl_minutes),
            )
        )
    return action_id


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def get_pending_action(action_id: str) -> PendingAction | None:
    """Return a still-valid pending action, or None."""
    with session_scope() as session:
        action = session.get(PendingAction, action_id)
        if action is None or action.status != "pending":
            return None
        if _as_utc(action.expires_at) <= utcnow():
            action.status = "expired"
            return None
        return action


def latest_pending_action() -> PendingAction | None:
    """Return the newest still-valid pending action."""
    with session_scope() as session:
        stmt = (
            select(PendingAction)
            .where(PendingAction.status == "pending")
            .order_by(PendingAction.created_at.desc())
        )
        action = session.scalars(stmt).first()
    if action is None or _as_utc(action.expires_at) <= utcnow():
        return None
    return action


def consume_pending_action(action_id: str) -> bool:
    """Mark an action as used so it cannot be replayed."""
    with session_scope() as session:
        action = session.get(PendingAction, action_id)
        if action is None or action.status != "pending":
            return False
        action.status = "used"
        return True
