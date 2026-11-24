"""KSENUS AI application entry point.

Phase 1 built the service shell; Phase 2 added conversations; this version
adds tasks, reminders, sleep checks, long-term memory, a Telegram user-account
layer, voice, the dashboard and an emergency stop — all on the same app.

Run locally:
    .\\.venv\\Scripts\\python.exe -m uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import base64
import io
import logging
import os
import secrets
import threading
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

import memory
import safety
from brain import BrainError
from config import PROJECT_ROOT, Settings, get_settings
from database import (
    EventLog,
    dispose_database,
    get_engine,
    init_database,
    record_event,
    session_scope,
)
from integrations.apyhub import ApyHubError
from logging_setup import configure_logging
from runtime import AgentRuntime, ChatOutcome
from schemas import (
    AgentRunOut,
    CallList,
    CallOut,
    ChatRequest,
    ChatResponse,
    ConfirmationOut,
    ConfirmationResult,
    ControlStatus,
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    DashboardSummary,
    ErrorResponse,
    HealthResponse,
    MemoryCreate,
    MemoryList,
    MemoryOut,
    MemoryUpdate,
    MessageOut,
    ReminderCreate,
    ReminderOut,
    ServiceStatus,
    SleepCheckCreate,
    SleepCheckOut,
    TaskCreate,
    TaskList,
    TaskOut,
    TaskUpdate,
    TelegramStatus,
    ToolExecutionOut,
    UsageOut,
    VoiceChatResponse,
    VoiceTranscriptResponse,
)
from voice.audio import AudioError, ensure_upload, to_ogg_opus
from voice.realtime import describe_protocol, serve_voice_session
from voice.speech_engine import VoiceError

__version__ = "0.3.0"
logger = logging.getLogger(__name__)

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
WEBAPP_DIR = PROJECT_ROOT / "web" / "mini-app"
MESSENGER_DIR = PROJECT_ROOT / "web" / "messenger"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}

def _build_app_archive() -> bytes:
    """The portable app as one ZIP, without secrets, databases or the venv."""
    from packaging_win import build_archive_bytes

    return build_archive_bytes()


def _build_installer() -> Path:
    """The Windows installer .exe the download button serves."""
    from packaging_win import build_installer_exe

    return build_installer_exe()


class CompanionStateRequest(BaseModel):
    """A state the companion should show, plus one short line about why."""

    state: str = Field(min_length=1, max_length=40)
    detail: str = Field(default="", max_length=200)


class CompanionDetailRequest(BaseModel):
    """Optional note shown next to the companion's state."""

    detail: str = Field(default="", max_length=200)


# --------------------------------------------------------------------------- #
# Security helpers
# --------------------------------------------------------------------------- #
def _is_loopback(request: Request) -> bool:
    client = request.client.host if request.client else ""
    return client in _LOOPBACK_HOSTS


def guard(request: Request) -> None:
    """Protect state-changing endpoints.

    With ``API_TOKEN`` configured, the header is mandatory. Without it, only
    loopback clients are allowed, so exposing the port on the LAN cannot
    accidentally hand control to a stranger.
    """
    settings: Settings = request.app.state.settings
    token = settings.api_token_value
    if token:
        supplied = request.headers.get("x-api-token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing X-API-Token",
            )
        return
    if not _is_loopback(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mutating endpoints require API_TOKEN for non-local clients",
        )


class RateLimiter:
    """Small in-memory sliding-window limiter for chat."""

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = limit_per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._events[key]
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= self._limit:
                return False
            bucket.append(now)
            return True


def get_runtime(request: Request) -> AgentRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - lifespan sets it before serving
        raise HTTPException(status_code=503, detail="agent is still starting")
    return runtime


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(
    settings: Settings | None = None,
    *,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_runtime = runtime or AgentRuntime(app_settings)
    limiter = RateLimiter(app_settings.chat_rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(app_settings)
        init_database(app_settings)
        app.state.runtime = app_runtime
        await app_runtime.start()
        record_event(
            source="startup",
            message=f"{app_settings.app_name} started (environment={app_settings.environment})",
            details=(
                f"provider={app_settings.llm_provider}, "
                f"model={app_settings.chat_model}, "
                f"openai_configured={app_settings.openai_configured}, "
                f"openrouter_configured={app_settings.openrouter_configured}, "
                f"telegram_enabled={app_settings.telegram_enabled}"
            ),
        )
        logger.info(
            "%s %s starting | timezone=%s | provider=%s | model=%s | telegram=%s",
            app_settings.app_name,
            __version__,
            app_settings.timezone,
            app_settings.llm_provider,
            app_settings.chat_model,
            app_settings.telegram_enabled,
        )
        try:
            yield
        finally:
            await app_runtime.stop()
            dispose_database()
            logger.info("%s stopped", app_settings.app_name)

    app = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        description=(
            "Personal AI assistant with conversation, memory, tasks, reminders, "
            "voice and a Telegram user-account layer."
        ),
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.runtime = app_runtime
    app.state.limiter = limiter

    if app_settings.dashboard_enabled and DASHBOARD_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")

    if WEBAPP_DIR.is_dir():
        app.mount("/mini-app/static", StaticFiles(directory=WEBAPP_DIR), name="mini-app")

        @app.get("/mini-app", include_in_schema=False)
        def mini_app() -> FileResponse:
            index = WEBAPP_DIR / "index.html"
            if not index.is_file():
                raise HTTPException(status_code=404, detail="mini app missing")
            return FileResponse(index)

    from messenger_api import router as messenger_router

    app.include_router(messenger_router, prefix="/messenger/api", tags=["messenger"])

    if MESSENGER_DIR.is_dir():
        app.mount(
            "/messenger/static",
            StaticFiles(directory=MESSENGER_DIR),
            name="messenger",
        )

        @app.get("/messenger", include_in_schema=False)
        def messenger_page() -> FileResponse:
            index = MESSENGER_DIR / "index.html"
            if not index.is_file():
                raise HTTPException(status_code=404, detail="messenger missing")
            return FileResponse(index)

    @app.get("/download/ksenos-setup.exe", include_in_schema=False)
    def download_installer() -> FileResponse:
        try:
            installer = _build_installer()
        except (OSError, RuntimeError) as exc:
            logger.warning("installer build failed: %s", exc)
            raise HTTPException(
                status_code=500, detail="the installer could not be built"
            ) from exc
        return FileResponse(
            installer,
            media_type="application/vnd.microsoft.portable-executable",
            filename="Ksenos-Setup.exe",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/download/ksenos-app.zip", include_in_schema=False)
    def download_app() -> Response:
        try:
            payload = _build_app_archive()
        except OSError as exc:
            logger.warning("download archive failed: %s", exc)
            raise HTTPException(status_code=500, detail="archive could not be built") from exc
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="Ksenos-Messenger.zip"',
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-store",
            },
        )

    # ------------------------------------------------------------------ #
    # System
    # ------------------------------------------------------------------ #
    @app.get("/", tags=["system"])
    def root() -> dict[str, Any]:
        return {
            "agent": app_settings.app_name,
            "version": __version__,
            "health": "/health",
            "docs": "/docs",
            "dashboard": "/dashboard",
            "mini_app": "/mini-app",
            "download": "/download/ksenos-setup.exe",
            "chat": "/chat",
            "tasks": "/tasks",
            "runs": "/runs",
            "voice": "/voice/chat",
            "images": "/images/remove-background",
            "companion": "/companion/state",
        }

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        try:
            get_engine()
            with session_scope() as session:
                session.execute(text("SELECT 1"))
                events_logged = session.scalar(select(func.count()).select_from(EventLog)) or 0
        except Exception as exc:
            logger.exception("Health check failed")
            raise HTTPException(status_code=503, detail="database unavailable") from exc

        return HealthResponse(
            status="ok",
            version=__version__,
            environment=app_settings.environment,
            timezone=app_settings.timezone,
            database="ok",
            openai_configured=app_settings.openai_configured,
            telegram_configured=app_settings.telegram_configured,
            paused=app_runtime.paused(),
            events_logged=int(events_logged),
            server_time_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # ------------------------------------------------------------------ #
    # Chat
    # ------------------------------------------------------------------ #
    def _chat_response(outcome: ChatOutcome) -> ChatResponse:
        return ChatResponse(
            conversation_id=outcome.conversation_id,
            reply=outcome.reply,
            model=outcome.model,
            usage=UsageOut(**outcome.usage) if outcome.usage else None,
            tools=[
                ToolExecutionOut(
                    tool=tool.name,
                    status=tool.status,
                    detail=tool.detail,
                    confirmation_id=tool.confirmation_id,
                )
                for tool in outcome.tools
            ],
            confirmation=(
                ConfirmationOut(
                    id=outcome.confirmation_id,
                    tool=outcome.confirmation_tool or "",
                    summary=outcome.confirmation_summary or "",
                )
                if outcome.confirmation_id
                else None
            ),
        )

    @app.post(
        "/chat",
        response_model=ChatResponse,
        tags=["chat"],
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    async def chat(
        payload: ChatRequest,
        request: Request,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> ChatResponse:
        client_key = request.client.host if request.client else "unknown"
        if not limiter.allow(client_key):
            raise HTTPException(status_code=429, detail="too many requests; wait a minute")

        try:
            outcome = await agent.chat(payload.message, payload.conversation_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BrainError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc
        return _chat_response(outcome)

    @app.post(
        "/confirmations/{action_id}",
        response_model=ConfirmationResult,
        tags=["chat"],
        dependencies=[Depends(guard)],
        responses={404: {"model": ErrorResponse}},
    )
    async def confirm(
        action_id: str,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> ConfirmationResult:
        result = await agent.execute_confirmation(action_id)
        if result is None:
            raise HTTPException(
                status_code=404, detail="confirmation not found, expired, or already used"
            )
        return ConfirmationResult(tool=result.name, status=result.status, detail=result.detail)

    # ------------------------------------------------------------------ #
    # Conversations
    # ------------------------------------------------------------------ #
    @app.get("/conversations", response_model=ConversationList, tags=["conversations"])
    async def list_conversations(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> ConversationList:
        stats, total = await to_thread.run_sync(memory.list_conversations, limit, offset)
        return ConversationList(
            conversations=[
                ConversationSummary(
                    id=item.id,
                    title=item.title,
                    message_count=item.message_count,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    last_message_at=item.last_message_at,
                )
                for item in stats
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/conversations/{conversation_id}",
        response_model=ConversationDetail,
        tags=["conversations"],
    )
    async def get_conversation(conversation_id: int) -> ConversationDetail:
        transcript = await to_thread.run_sync(memory.get_transcript, conversation_id)
        if transcript is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        conversation, messages = transcript
        return ConversationDetail(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            messages=[MessageOut.model_validate(message) for message in messages],
        )

    @app.delete(
        "/conversations/{conversation_id}",
        status_code=204,
        response_class=Response,
        tags=["conversations"],
        dependencies=[Depends(guard)],
    )
    async def delete_conversation(conversation_id: int) -> Response:
        deleted = await to_thread.run_sync(memory.delete_conversation, conversation_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="conversation not found")
        return Response(status_code=204)

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    @app.get("/tasks", response_model=TaskList, tags=["tasks"])
    async def list_tasks(
        status_filter: str = Query(default="all", alias="status", pattern="^(all|pending|done|cancelled)$"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        agent: AgentRuntime = Depends(get_runtime),
    ) -> TaskList:
        tasks, total, pending = await to_thread.run_sync(
            agent.tasks.list, status_filter, limit, offset
        )
        return TaskList(
            tasks=[TaskOut.model_validate(task) for task in tasks],
            total=total,
            pending=pending,
        )

    @app.post(
        "/tasks",
        response_model=TaskOut,
        status_code=201,
        tags=["tasks"],
        dependencies=[Depends(guard)],
    )
    async def create_task(
        payload: TaskCreate,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> TaskOut:
        task = await to_thread.run_sync(
            lambda: agent.tasks.create(
                payload.title,
                details=payload.details,
                priority=payload.priority,
                due_at=payload.due_at,
                source="api",
            )
        )
        return TaskOut.model_validate(task)

    @app.patch(
        "/tasks/{task_id}",
        response_model=TaskOut,
        tags=["tasks"],
        dependencies=[Depends(guard)],
    )
    async def update_task(
        task_id: int,
        payload: TaskUpdate,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> TaskOut:
        changes = payload.model_dump(exclude_unset=True)
        try:
            task = await to_thread.run_sync(
                lambda: agent.tasks.update(task_id, **changes)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskOut.model_validate(task)

    @app.post(
        "/tasks/{task_id}/complete",
        response_model=TaskOut,
        tags=["tasks"],
        dependencies=[Depends(guard)],
    )
    async def complete_task(
        task_id: int,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> TaskOut:
        task = await to_thread.run_sync(agent.tasks.complete, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskOut.model_validate(task)

    @app.delete(
        "/tasks/{task_id}",
        status_code=204,
        response_class=Response,
        tags=["tasks"],
        dependencies=[Depends(guard)],
    )
    async def delete_task(
        task_id: int,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> Response:
        deleted = await to_thread.run_sync(agent.tasks.delete, task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="task not found")
        return Response(status_code=204)

    # ------------------------------------------------------------------ #
    # Reminders and sleep checks
    # ------------------------------------------------------------------ #
    @app.get("/reminders", response_model=list[ReminderOut], tags=["reminders"])
    async def list_reminders(
        status_filter: str = Query(default="all", alias="status"),
        agent: AgentRuntime = Depends(get_runtime),
    ) -> list[ReminderOut]:
        rows = await to_thread.run_sync(agent.reminders.list, status_filter, 50)
        return [ReminderOut.model_validate(row) for row in rows]

    @app.post(
        "/reminders",
        response_model=ReminderOut,
        status_code=201,
        tags=["reminders"],
        dependencies=[Depends(guard)],
    )
    async def create_reminder(
        payload: ReminderCreate,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> ReminderOut:
        reminder = await to_thread.run_sync(
            lambda: agent.reminders.create(
                payload.message, payload.remind_at, task_id=payload.task_id
            )
        )
        return ReminderOut.model_validate(reminder)

    @app.post(
        "/reminders/{reminder_id}/acknowledge",
        response_model=ReminderOut,
        tags=["reminders"],
        dependencies=[Depends(guard)],
    )
    async def acknowledge_reminder(
        reminder_id: int,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> ReminderOut:
        acknowledged = await to_thread.run_sync(agent.reminders.acknowledge, reminder_id)
        if not acknowledged:
            raise HTTPException(status_code=404, detail="reminder not found")
        reminder = await to_thread.run_sync(agent.reminders.get, reminder_id)
        return ReminderOut.model_validate(reminder)

    @app.delete(
        "/reminders/{reminder_id}",
        status_code=204,
        response_class=Response,
        tags=["reminders"],
        dependencies=[Depends(guard)],
    )
    async def cancel_reminder(
        reminder_id: int,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> Response:
        if not await to_thread.run_sync(agent.reminders.cancel, reminder_id):
            raise HTTPException(status_code=404, detail="reminder not found")
        return Response(status_code=204)

    @app.post(
        "/sleep/check",
        response_model=SleepCheckOut,
        status_code=201,
        tags=["sleep"],
        dependencies=[Depends(guard)],
    )
    async def start_sleep_check(
        payload: SleepCheckCreate,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> SleepCheckOut:
        check = await to_thread.run_sync(agent.sleep.start, payload.minutes, payload.label)
        return SleepCheckOut.model_validate(check)

    @app.get(
        "/sleep/check",
        response_model=SleepCheckOut | None,
        tags=["sleep"],
    )
    async def latest_sleep_check(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> SleepCheckOut | None:
        check = await to_thread.run_sync(agent.sleep.latest_active)
        return SleepCheckOut.model_validate(check) if check is not None else None

    @app.post(
        "/sleep/acknowledge",
        response_model=SleepCheckOut | None,
        tags=["sleep"],
        dependencies=[Depends(guard)],
    )
    async def acknowledge_sleep(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> SleepCheckOut | None:
        check = await to_thread.run_sync(agent.sleep.record_response)
        return SleepCheckOut.model_validate(check) if check is not None else None

    # ------------------------------------------------------------------ #
    # Memory
    # ------------------------------------------------------------------ #
    @app.get("/memory", response_model=MemoryList, tags=["memory"])
    async def list_memory(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        category: str | None = Query(default=None),
    ) -> MemoryList:
        entries, total = await to_thread.run_sync(
            memory.list_memories, limit, offset, category
        )
        return MemoryList(
            memories=[MemoryOut.model_validate(entry) for entry in entries],
            total=total,
        )

    @app.get("/memory/search", response_model=list[MemoryOut], tags=["memory"])
    async def search_memory(q: str = Query(min_length=1, max_length=200)) -> list[MemoryOut]:
        entries = await to_thread.run_sync(memory.search_memories, q, 20)
        return [MemoryOut.model_validate(entry) for entry in entries]

    @app.post(
        "/memory",
        response_model=MemoryOut,
        status_code=201,
        tags=["memory"],
        dependencies=[Depends(guard)],
    )
    async def create_memory(payload: MemoryCreate) -> MemoryOut:
        entry = await to_thread.run_sync(
            lambda: memory.add_memory(payload.content, payload.category, source="api")
        )
        return MemoryOut.model_validate(entry)

    @app.patch(
        "/memory/{memory_id}",
        response_model=MemoryOut,
        tags=["memory"],
        dependencies=[Depends(guard)],
    )
    async def update_memory(memory_id: int, payload: MemoryUpdate) -> MemoryOut:
        entry = await to_thread.run_sync(
            lambda: memory.update_memory(
                memory_id, content=payload.content, category=payload.category
            )
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="memory not found")
        return MemoryOut.model_validate(entry)

    @app.delete(
        "/memory/{memory_id}",
        status_code=204,
        response_class=Response,
        tags=["memory"],
        dependencies=[Depends(guard)],
    )
    async def delete_memory(memory_id: int) -> Response:
        if not await to_thread.run_sync(memory.delete_memory, memory_id):
            raise HTTPException(status_code=404, detail="memory not found")
        return Response(status_code=204)

    # ------------------------------------------------------------------ #
    # Calls and Telegram
    # ------------------------------------------------------------------ #
    @app.get("/calls", response_model=CallList, tags=["calls"])
    async def list_calls(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> CallList:
        rows, total = await to_thread.run_sync(agent.calls.history, 50)
        return CallList(calls=[CallOut.model_validate(row) for row in rows], total=total)

    @app.get("/calls/diagnostics", tags=["calls"])
    def call_diagnostics(agent: AgentRuntime = Depends(get_runtime)) -> dict[str, Any]:
        return agent.calls.transport_diagnostics()

    @app.get("/calls/status", tags=["calls"])
    def call_status(agent: AgentRuntime = Depends(get_runtime)) -> dict[str, Any]:
        return agent.calls.status()

    @app.post(
        "/calls/answer",
        tags=["calls"],
        dependencies=[Depends(guard)],
    )
    async def answer_call(agent: AgentRuntime = Depends(get_runtime)) -> dict[str, Any]:
        answered, detail = await agent.calls.answer()
        return {"answered": answered, "detail": detail}

    @app.post(
        "/calls/hangup",
        tags=["calls"],
        dependencies=[Depends(guard)],
    )
    async def hangup_call(agent: AgentRuntime = Depends(get_runtime)) -> dict[str, Any]:
        ended, detail = await agent.calls.hangup()
        return {"ended": ended, "detail": detail}

    # -- computer mode -------------------------------------------------- #
    @app.get(
        "/computer-mode/state",
        tags=["computer"],
        dependencies=[Depends(guard)],
    )
    async def computer_mode_state(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """What the computer-mode window shows; also closes a finished mode."""
        await agent.computer_mode.refresh()
        return agent.computer_mode.state()

    @app.post(
        "/computer-mode/stop",
        tags=["computer"],
        dependencies=[Depends(guard)],
    )
    async def computer_mode_stop(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        stopped, detail = await agent.computer_mode.stop()
        return {"stopped": stopped, "detail": detail}

    # -- desktop companion ----------------------------------------------- #
    @app.get(
        "/companion/state",
        tags=["companion"],
        dependencies=[Depends(guard)],
    )
    def companion_state(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """What the pixel companion draws; polled by its own process."""
        return agent.companion.state()

    @app.post(
        "/companion/state",
        tags=["companion"],
        dependencies=[Depends(guard)],
    )
    def companion_set_state(
        payload: CompanionStateRequest,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """Put the companion in a state by name (thinking, speaking, happy…)."""
        if not agent.companion.set_state(payload.state, payload.detail):
            raise HTTPException(
                status_code=422,
                detail=(
                    "unknown companion state; use one of: "
                    "idle, idle_cute, thinking, speaking, listening, happy, sad, "
                    "surprised, sleep, walk, run, fly, hover, take_off, land, "
                    "follow_mouse, jump, click_reaction"
                ),
            )
        return agent.companion.state()

    @app.post(
        "/companion/show",
        tags=["companion"],
        dependencies=[Depends(guard)],
    )
    def companion_show(
        payload: CompanionDetailRequest | None = None,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """Put the pet on screen now."""
        agent.companion.show((payload.detail if payload else "") or "egasi ko‘rsatdi")
        return agent.companion.state()

    @app.post(
        "/companion/hide",
        tags=["companion"],
        dependencies=[Depends(guard)],
    )
    def companion_hide(
        payload: CompanionDetailRequest | None = None,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """Take the pet off the screen until it is shown again."""
        agent.companion.hide((payload.detail if payload else "") or "egasi yashirdi")
        return agent.companion.state()

    @app.post(
        "/companion/event",
        tags=["companion"],
        dependencies=[Depends(guard)],
    )
    def companion_event(
        payload: CompanionStateRequest,
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """Drive the pet with a real event name (ai_thinking_started, …)."""
        if not agent.companion.event(payload.state, payload.detail):
            raise HTTPException(status_code=422, detail="unknown companion event")
        return agent.companion.state()

    # -- animated cursor ------------------------------------------------- #
    @app.get(
        "/gui/cursor/state",
        tags=["computer"],
        dependencies=[Depends(guard)],
    )
    def gui_cursor_state(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> dict[str, Any]:
        """What the animated cursor layer draws; polled by its own process."""
        return agent.gui.cursor_state()

    @app.get("/telegram/status", response_model=TelegramStatus, tags=["telegram"])
    def telegram_status(agent: AgentRuntime = Depends(get_runtime)) -> TelegramStatus:
        return TelegramStatus(**agent.telegram.status())

    # ------------------------------------------------------------------ #
    # Agent runs
    # ------------------------------------------------------------------ #
    @app.get("/runs", response_model=list[AgentRunOut], tags=["runs"])
    async def list_agent_runs(
        limit: int = Query(default=20, ge=1, le=100),
        agent: AgentRuntime = Depends(get_runtime),
    ) -> list[AgentRunOut]:
        """What the agent actually did: trigger, channel, result and duration."""
        rows = await to_thread.run_sync(agent.runs.list, limit)
        return [AgentRunOut.model_validate(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    @app.get("/control/status", response_model=ControlStatus, tags=["control"])
    def control_status(agent: AgentRuntime = Depends(get_runtime)) -> ControlStatus:
        return ControlStatus(**agent.status())

    @app.post(
        "/control/stop",
        response_model=ControlStatus,
        tags=["control"],
        dependencies=[Depends(guard)],
    )
    async def emergency_stop(agent: AgentRuntime = Depends(get_runtime)) -> ControlStatus:
        agent.set_paused(True)
        return ControlStatus(**agent.status())

    @app.post(
        "/control/resume",
        response_model=ControlStatus,
        tags=["control"],
        dependencies=[Depends(guard)],
    )
    async def resume(agent: AgentRuntime = Depends(get_runtime)) -> ControlStatus:
        agent.set_paused(False)
        return ControlStatus(**agent.status())

    # ------------------------------------------------------------------ #
    # Voice
    # ------------------------------------------------------------------ #
    @app.post(
        "/voice/chat",
        response_model=VoiceChatResponse,
        tags=["voice"],
        dependencies=[Depends(guard)],
        responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    )
    async def voice_chat(
        file: UploadFile = File(..., description="Recorded speech (webm, ogg, mp3, wav)."),
        agent: AgentRuntime = Depends(get_runtime),
    ) -> VoiceChatResponse:
        audio = await file.read()
        try:
            ensure_upload(file.filename, audio, app_settings.voice_max_upload_mb)
            transcript = await agent.speech.transcribe(
                audio, filename=file.filename or "utterance.webm"
            )
        except AudioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except VoiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc

        if not transcript.strip():
            return VoiceChatResponse(
                transcript="",
                reply="I did not catch any speech. Please try again.",
                conversation_id=0,
            )

        try:
            outcome = await agent.chat(transcript)
        except BrainError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc

        audio_reply: bytes | None = None
        try:
            audio_reply = await agent.speech.synthesize(outcome.reply)
        except (VoiceError, ValueError):
            logger.warning("Voice reply could not be synthesised", exc_info=True)

        return VoiceChatResponse(
            transcript=transcript,
            reply=outcome.reply,
            conversation_id=outcome.conversation_id,
            audio_mime="audio/mpeg" if audio_reply else None,
            audio_base64=base64.b64encode(audio_reply).decode("ascii") if audio_reply else None,
        )

    @app.post(
        "/voice/transcribe",
        response_model=VoiceTranscriptResponse,
        tags=["voice"],
        dependencies=[Depends(guard)],
        responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    )
    async def voice_transcribe(
        file: UploadFile = File(..., description="Recorded speech (wav, webm, ogg, mp3)."),
        agent: AgentRuntime = Depends(get_runtime),
    ) -> VoiceTranscriptResponse:
        """Only the transcript, for a client that owns its conversation.

        The Windows mini messenger keeps one chat thread open: it records the
        owner, asks for the words here, then sends them to ``POST /chat`` with
        its own ``conversation_id``. ``/voice/chat`` would start a new thread on
        every turn and lose that context.
        """
        audio = await file.read()
        try:
            ensure_upload(file.filename, audio, app_settings.voice_max_upload_mb)
            text = await agent.speech.transcribe(
                audio, filename=file.filename or "utterance.wav"
            )
        except AudioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except VoiceError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=exc.public_message
            ) from exc
        return VoiceTranscriptResponse(text=text.strip())

    @app.post(
        "/voice/speak",
        tags=["voice"],
        dependencies=[Depends(guard)],
        responses={200: {"content": {"audio/mpeg": {}}}, 502: {"model": ErrorResponse}},
    )
    async def speak(
        payload: dict[str, str],
        agent: AgentRuntime = Depends(get_runtime),
    ) -> Response:
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="text is required")
        try:
            audio = await agent.speech.synthesize(text)
        except VoiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc
        return Response(content=audio, media_type="audio/mpeg")

    @app.post(
        "/voice/note",
        tags=["voice"],
        dependencies=[Depends(guard)],
        responses={200: {"content": {"audio/ogg": {}}}},
    )
    async def voice_note(
        payload: dict[str, str],
        agent: AgentRuntime = Depends(get_runtime),
    ) -> Response:
        """Render text as an OGG/Opus voice note for Telegram."""
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="text is required")
        try:
            mp3 = await agent.speech.synthesize(text)
            ogg = await to_thread.run_sync(to_ogg_opus, mp3)
        except VoiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc
        except AudioError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return Response(content=ogg, media_type="audio/ogg")

    @app.websocket("/ws/voice")
    async def voice_socket(websocket: WebSocket) -> None:
        app_settings_ws: Settings = websocket.app.state.settings
        if app_settings_ws.api_token_value:
            supplied = websocket.query_params.get("token", "")
            if not secrets.compare_digest(supplied, app_settings_ws.api_token_value):
                await websocket.close(code=4401)
                return
        agent: AgentRuntime = websocket.app.state.runtime
        await serve_voice_session(websocket, agent.speech, agent.brain, app_settings_ws)

    @app.get("/voice/protocol", tags=["voice"])
    def voice_protocol() -> dict[str, Any]:
        return describe_protocol()

    # ------------------------------------------------------------------ #
    # Images
    # ------------------------------------------------------------------ #
    @app.post(
        "/images/remove-background",
        tags=["images"],
        dependencies=[Depends(guard)],
        responses={
            200: {"content": {"image/png": {}}, "description": "Transparent PNG cutout"},
            400: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def remove_image_background(
        file: UploadFile = File(
            ..., description="Raster image: PNG, JPEG, WebP, GIF, BMP or TIFF."
        ),
        agent: AgentRuntime = Depends(get_runtime),
    ) -> Response:
        """Remove one uploaded image's background; returns the PNG bytes.

        The upload is validated before anything is sent, and the original bytes
        are only ever read: nothing on disk is changed by this endpoint.
        """
        data = await file.read()
        try:
            cutout = await agent.images.remove_background(data)
        except ApyHubError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        stem = Path(file.filename or "image").stem
        safe = "".join(
            char for char in stem if char.isascii() and (char.isalnum() or char in "-_")
        ) or "image"
        checked = "" if cutout.transparent_pixels is None else str(cutout.transparent_pixels)
        return Response(
            content=cutout.png,
            media_type="image/png",
            headers={
                "Content-Disposition": f'inline; filename="{safe}-no-bg.png"',
                "X-Image-Transparent-Pixels": checked,
            },
        )

    # ------------------------------------------------------------------ #
    # Dashboard
    # ------------------------------------------------------------------ #
    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        if not app_settings.dashboard_enabled:
            raise HTTPException(status_code=404, detail="dashboard disabled")
        index = DASHBOARD_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="dashboard assets missing")
        return FileResponse(index)

    @app.get("/dashboard/summary", response_model=DashboardSummary, tags=["dashboard"])
    async def dashboard_summary(
        agent: AgentRuntime = Depends(get_runtime),
    ) -> DashboardSummary:
        conversations, _ = await to_thread.run_sync(memory.list_conversations, 1, 0)
        tasks, task_total, task_pending = await to_thread.run_sync(agent.tasks.list, "all", 5, 0)
        reminders = await to_thread.run_sync(agent.reminders.list, "pending", 100)
        memories = await to_thread.run_sync(memory.memory_stats)
        calls, call_total = await to_thread.run_sync(agent.calls.history, 5)

        with session_scope() as session:
            events = session.execute(
                select(EventLog).order_by(EventLog.id.desc()).limit(10)
            ).scalars().all()
            conversation_total = session.scalar(
                select(func.count()).select_from(memory.Conversation)
            ) or 0

        return DashboardSummary(
            agent=app_settings.app_name,
            version=__version__,
            status=ControlStatus(**agent.status()),
            services=ServiceStatus(**agent.services()),
            counts={
                "tasks_total": task_total,
                "tasks_pending": task_pending,
                "reminders_pending": len(reminders),
                "conversations": int(conversation_total),
                "calls": call_total,
            },
            upcoming_tasks=[TaskOut.model_validate(task) for task in tasks],
            recent_activity=[
                {
                    "id": event.id,
                    "at": event.created_at.isoformat(),
                    "level": event.level,
                    "source": event.source,
                    "message": event.message,
                }
                for event in events
            ],
            recent_calls=[CallOut.model_validate(row) for row in calls],
            memory_total=memories.get("total", 0),
        )

    @app.get("/activity", tags=["dashboard"])
    async def activity(limit: int = 40) -> dict[str, Any]:
        """The newest tool calls, exactly as they were executed.

        ``data/audit.jsonl`` is written by the tool registry itself, so this
        list is what really ran — not what a summary remembered.
        """
        count = max(1, min(int(limit or 40), 200))
        return {"actions": await to_thread.run_sync(safety.recent_audit, count)}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    uvicorn.run("main:app", host=_settings.api_host, port=_settings.api_port, reload=False)
