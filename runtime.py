"""Application runtime.

One object owns the long-lived services, so FastAPI routes stay thin and the
Telegram handler can reuse exactly the same chat path as the HTTP API. Nothing
here fabricates a capability: each service reports whether it is actually able
to do its job.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from anyio import to_thread

import memory
from brain import AgentBrain, BrainError, ToolResult
from config import Settings
from database import record_event
from tasks.manager import TaskManager
from tasks.reminders import ReminderService
from tasks.scheduler import AgentScheduler
from tasks.sleep_monitor import SleepMonitor
from telegram.account import TelegramAccount
from telegram.calls import CallManager
from tools import ToolContext, build_tool_registry
from voice.speech_engine import SpeechEngine

logger = logging.getLogger(__name__)


@dataclass
class ChatOutcome:
    """Everything a chat call produced, shared by HTTP and Telegram."""

    conversation_id: int
    reply: str
    model: str
    usage: dict[str, int] | None
    tools: list[ToolResult]
    confirmation_id: str | None = None
    confirmation_tool: str | None = None
    confirmation_summary: str | None = None


class AgentRuntime:
    """Owns services and the shared chat pipeline."""

    def __init__(
        self,
        settings: Settings,
        *,
        brain: AgentBrain | None = None,
        telegram: TelegramAccount | None = None,
        speech: SpeechEngine | None = None,
    ) -> None:
        self.settings = settings
        self.started_at = datetime.now(timezone.utc)

        self.tasks = TaskManager()

        reminder_holder: dict[str, ReminderService] = {}

        async def run_reminder(reminder_id: int) -> None:
            await reminder_holder["service"].run_due(reminder_id)

        self.scheduler = AgentScheduler(settings, run_reminder)

        account_holder: dict[str, TelegramAccount] = {}

        async def notify(text: str) -> bool:
            account = account_holder.get("account")
            delivered = bool(account and account.ready and await account.send_message(text))
            record_event(
                source="notifier",
                message="Notification delivered" if delivered else "Notification not delivered",
                level="INFO" if delivered else "WARNING",
                details=text[:500],
            )
            return delivered

        self.reminders = ReminderService(settings, self.scheduler, notify)
        reminder_holder["service"] = self.reminders

        self.sleep = SleepMonitor(settings, self.reminders)
        self.reminders.set_exhausted_hook(self.sleep.on_reminder_exhausted)

        self.telegram = telegram or TelegramAccount(
            settings,
            on_message=self.handle_telegram_message,
            on_call=self.handle_call_update,
        )
        account_holder["account"] = self.telegram
        self.calls = CallManager(settings, self.telegram)
        self.speech = speech or SpeechEngine(settings)

        self.tasks.bind_reminders(self.reminders)

        context = ToolContext(
            settings=settings,
            tasks=self.tasks,
            reminders=self.reminders,
            sleep=self.sleep,
            calls=self.calls,
        )
        registry = build_tool_registry(context)

        self.brain = brain or AgentBrain(settings, registry=registry)
        self.brain.set_tool_registry(registry)

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        self.scheduler.start()
        if memory.is_paused():
            self.scheduler.pause()
        self.reminders.restore()
        detail = await self.telegram.start()
        record_event(
            source="runtime",
            message="Runtime started",
            details=f"telegram={detail}, paused={memory.is_paused()}",
        )

    async def stop(self) -> None:
        await self.telegram.stop()
        self.scheduler.shutdown()
        record_event(source="runtime", message="Runtime stopped")

    # -- chat ------------------------------------------------------------- #
    async def chat(self, message: str, conversation_id: int | None = None) -> ChatOutcome:
        """Persist the turn, ask the brain, persist the answer."""
        if conversation_id is None:
            conversation = await to_thread.run_sync(memory.create_conversation, message)
            history: list[Any] = []
        else:
            conversation = await to_thread.run_sync(
                memory.get_conversation, conversation_id
            )
            if conversation is None:
                raise LookupError(f"Conversation {conversation_id} not found")
            history = await to_thread.run_sync(
                memory.recent_messages,
                conversation.id,
                self.settings.chat_context_messages,
            )

        await to_thread.run_sync(memory.add_message, conversation.id, "user", message)

        try:
            result = await self.brain.reply(
                history=[{"role": item.role, "content": item.content} for item in history],
                user_message=message,
            )
        except BrainError as exc:
            await to_thread.run_sync(
                lambda: record_event(
                    source="chat",
                    message="Chat request failed",
                    level="ERROR",
                    details=f"conversation_id={conversation.id}, reason={exc.public_message}",
                )
            )
            raise

        await to_thread.run_sync(
            memory.add_message, conversation.id, "assistant", result.text
        )

        outcome = ChatOutcome(
            conversation_id=conversation.id,
            reply=result.text,
            model=result.model,
            usage=result.usage,
            tools=result.tool_results,
        )

        for tool in result.tool_results:
            if tool.confirmation_id:
                action = await to_thread.run_sync(
                    memory.get_pending_action, tool.confirmation_id
                )
                outcome.confirmation_id = tool.confirmation_id
                outcome.confirmation_tool = tool.name
                outcome.confirmation_summary = (
                    action.summary if action is not None else tool.detail
                )
                break

        return outcome

    async def execute_confirmation(self, action_id: str) -> ToolResult | None:
        """Execute a confirmed pending action exactly once."""
        action = await to_thread.run_sync(memory.get_pending_action, action_id)
        if action is None:
            return None

        try:
            arguments = json.loads(action.arguments) if action.arguments else {}
        except ValueError:
            arguments = {}

        result = await self.brain.tools.execute(action.tool, arguments, confirmed=True)
        await to_thread.run_sync(memory.consume_pending_action, action_id)
        await to_thread.run_sync(
            lambda: record_event(
                source="confirmation",
                message=f"Confirmed action {action.tool} executed",
                details=f"status={result.status}",
            )
        )
        return result

    # -- telegram --------------------------------------------------------- #
    async def handle_telegram_message(self, text: str) -> str | None:
        """Handle an owner message: chat, acknowledge checks, confirm actions."""
        lowered = text.strip().lower()
        active_sleep = await to_thread.run_sync(self.sleep.latest_active)

        if lowered in {"confirm", "yes", "ha", "tasdiq", "ok"}:
            pending = await to_thread.run_sync(memory.latest_pending_action)
            if pending is not None:
                result = await self.execute_confirmation(pending.id)
                if result is not None:
                    return f"Confirmed: {result.detail}"
                return "That confirmation expired; nothing was executed."

        if active_sleep is not None:
            responded = await to_thread.run_sync(self.sleep.record_response)
            if responded is not None:
                await to_thread.run_sync(self.reminders.acknowledge_all_pending)

        try:
            outcome = await self.chat(text)
        except BrainError as exc:
            return exc.public_message
        except LookupError:
            return "I could not find that conversation."

        if outcome.confirmation_id:
            return (
                f"{outcome.reply}\n\nThis needs your confirmation. Reply 'confirm' "
                "here or approve it in the dashboard."
            )
        return outcome.reply

    async def handle_call_update(self, update: Any) -> None:
        await self.calls.handle_update(update)

    # -- control ---------------------------------------------------------- #
    def paused(self) -> bool:
        return memory.is_paused()

    def set_paused(self, paused: bool) -> None:
        memory.set_paused(paused)
        if paused:
            self.scheduler.pause()
        else:
            self.scheduler.resume()
        record_event(
            source="control",
            message="Emergency stop engaged" if paused else "Autonomy resumed",
            level="WARNING" if paused else "INFO",
        )

    def status(self) -> dict[str, Any]:
        return {
            "paused": memory.is_paused(),
            "scheduler_running": self.scheduler.running,
            "telegram_connected": self.telegram.ready,
            "telegram_configured": self.settings.telegram_configured,
            "live_calls_enabled": self.settings.telegram_calls_enabled,
            "openai_configured": self.settings.openai_configured,
            "timezone": self.settings.timezone,
        }

    def services(self) -> dict[str, bool]:
        return {
            "brain": True,
            "tasks": True,
            "scheduler": self.scheduler.running,
            "telegram_messaging": self.telegram.ready,
            "telegram_calls_signaling": self.settings.telegram_configured,
            "telegram_calls_audio": self.calls.can_carry_audio(),
            "speech_to_text": self.speech.configured,
            "text_to_speech": self.speech.configured,
            "dashboard": self.settings.dashboard_enabled,
        }
