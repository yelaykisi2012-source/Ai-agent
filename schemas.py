"""Request and response schemas for the HTTP API.

Kept separate from ORM models so database changes never leak straight into
the public contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    """A single user turn."""

    message: str = Field(
        min_length=1,
        max_length=4000,
        description="The user's message. Uzbek or English.",
        examples=["Ertaga soat 9 da meni uyg'ot."],
    )
    conversation_id: int | None = Field(
        default=None,
        ge=1,
        description="Existing conversation to continue. Omit to start a new one.",
    )

    @field_validator("message")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped


class ToolExecutionOut(BaseModel):
    """Outcome of one tool the model tried to use."""

    tool: str
    status: str = Field(description="ok | unavailable | confirmation_required | error")
    detail: str
    confirmation_id: str | None = None


class UsageOut(BaseModel):
    """Token accounting reported by the model provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ConfirmationOut(BaseModel):
    """A sensitive action awaiting the user's explicit approval."""

    id: str
    tool: str
    summary: str


class ChatResponse(BaseModel):
    """The agent's answer plus identifiers needed to continue the thread."""

    conversation_id: int
    reply: str
    model: str
    usage: UsageOut | None = None
    tools: list[ToolExecutionOut] = Field(default_factory=list)
    confirmation: ConfirmationOut | None = None


class ConfirmationResult(BaseModel):
    """Result of executing a confirmed action."""

    tool: str
    status: str
    detail: str


class MessageOut(BaseModel):
    """A persisted message."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime


class ConversationSummary(BaseModel):
    """Conversation row for list responses, without the transcript."""

    id: int
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


class ConversationList(BaseModel):
    """Paginated conversation list."""

    conversations: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class ConversationDetail(BaseModel):
    """A conversation with its full transcript."""

    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


# --------------------------------------------------------------------------- #
# Tasks and reminders
# --------------------------------------------------------------------------- #
class TaskCreate(BaseModel):
    """Create a task."""

    title: str = Field(min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=4000)
    priority: Literal["low", "normal", "high"] = "normal"
    due_at: datetime | None = Field(
        default=None, description="Deadline. Naive values are interpreted as UTC."
    )

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned


class TaskUpdate(BaseModel):
    """Update any subset of a task's fields."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=4000)
    priority: Literal["low", "normal", "high"] | None = None
    due_at: datetime | None = None
    status: Literal["pending", "done", "cancelled"] | None = None


class TaskOut(BaseModel):
    """A task as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    details: str | None
    priority: str
    status: str
    due_at: datetime | None
    source: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class TaskList(BaseModel):
    """Task list with counts."""

    tasks: list[TaskOut]
    total: int
    pending: int


class ReminderCreate(BaseModel):
    """Schedule a reminder."""

    message: str = Field(min_length=1, max_length=1000)
    remind_at: datetime = Field(description="When to notify. Naive values are UTC.")
    task_id: int | None = Field(default=None, ge=1)


class ReminderOut(BaseModel):
    """A scheduled reminder."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int | None
    kind: str
    message: str
    remind_at: datetime
    status: str
    attempts: int
    max_attempts: int
    interval_minutes: int
    created_at: datetime


class SleepCheckCreate(BaseModel):
    """Start a sleep check."""

    minutes: int = Field(default=30, ge=1, le=720)
    label: str = Field(default="sleep", max_length=100)


class SleepCheckOut(BaseModel):
    """A sleep check and its honest outcome."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    expected_wake_at: datetime
    status: str
    responded_at: datetime | None
    attempts: int
    notes: str | None


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
class MemoryCreate(BaseModel):
    """Save a durable memory."""

    content: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="general", max_length=32)


class MemoryUpdate(BaseModel):
    """Update a stored memory."""

    content: str | None = Field(default=None, min_length=1, max_length=4000)
    category: str | None = Field(default=None, max_length=32)


class MemoryOut(BaseModel):
    """A stored memory."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    content: str
    source: str
    created_at: datetime
    updated_at: datetime


class MemoryList(BaseModel):
    """Memory list."""

    memories: list[MemoryOut]
    total: int


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
class CallOut(BaseModel):
    """A call history row."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: str
    peer_id: int | None
    state: str
    reason: str | None
    started_at: datetime
    ended_at: datetime | None


class CallList(BaseModel):
    """Call history."""

    calls: list[CallOut]
    total: int


# --------------------------------------------------------------------------- #
# Control and status
# --------------------------------------------------------------------------- #
class ControlStatus(BaseModel):
    """Emergency-stop state and scheduler visibility."""

    paused: bool
    scheduler_running: bool
    telegram_connected: bool
    telegram_configured: bool
    live_calls_enabled: bool
    openai_configured: bool
    timezone: str


class ServiceStatus(BaseModel):
    """What each subsystem can actually do right now."""

    brain: bool
    tasks: bool
    scheduler: bool
    telegram_messaging: bool
    telegram_calls_signaling: bool
    telegram_calls_audio: bool
    speech_to_text: bool
    text_to_speech: bool
    dashboard: bool


class DashboardSummary(BaseModel):
    """Everything the local dashboard renders."""

    agent: str
    version: str
    status: ControlStatus
    services: ServiceStatus
    counts: dict[str, int]
    upcoming_tasks: list[TaskOut]
    recent_activity: list[dict[str, Any]]
    recent_calls: list[CallOut]
    memory_total: int


class AgentRunOut(BaseModel):
    """One recorded agent turn, as shown in the run log."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    channel: str
    status: str
    prompt: str = ""
    reply: str = ""
    tools: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class TelegramStatus(BaseModel):
    """Telegram connection state, never including session data."""

    enabled: bool
    configured: bool
    connected: bool
    authorized: bool
    session_present: bool = False
    owner_configured: bool
    live_calls_enabled: bool
    detail: str


class ErrorResponse(BaseModel):
    """Error body returned by FastAPI for 4xx/5xx responses."""

    detail: str


class VoiceChatResponse(BaseModel):
    """Result of a one-shot voice turn."""

    transcript: str
    reply: str
    conversation_id: int
    audio_mime: str | None = None
    audio_base64: str | None = None


class VoiceTranscriptResponse(BaseModel):
    """Only the words, for a client that keeps its own conversation."""

    text: str


class HealthResponse(BaseModel):
    """Payload returned by ``GET /health``."""

    status: str
    version: str
    environment: str
    timezone: str
    database: str
    openai_configured: bool
    telegram_configured: bool
    paused: bool
    events_logged: int
    server_time_utc: str
