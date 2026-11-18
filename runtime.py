"""Application runtime.

One object owns the long-lived services, so FastAPI routes stay thin and the
Telegram handler can reuse exactly the same chat path as the HTTP API. Nothing
here fabricates a capability: each service reports whether it is actually able
to do its job.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
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
from telegram.delivery import TelegramDelivery
from tools import ToolContext, build_tool_registry
from voice.speech_engine import SpeechEngine, build_speech_engine

logger = logging.getLogger(__name__)

#: A call conversation older than this is treated as a different call.
_CALL_CONVERSATION_IDLE_SECONDS = 600.0
#: A text conversation older than this starts over, per person.
_CHAT_CONVERSATION_IDLE_SECONDS = 1800.0
#: How long a "may I call you?" offer waits for an answer.
_CALL_OFFER_SECONDS = 600.0

#: A guest asking to be called, in the languages Ksenus hears most.
_CALL_REQUEST_RE = re.compile(
    r"(qo[‘'\u02bb]ng[‘'\u02bb]iroq|telefon qiling|call me|give me a call|позвони|звони)",
    re.IGNORECASE,
)
#: Explicit agreement. Anchored at the start so "hayot haqida" is not a yes.
_YES_RE = re.compile(
    r"^(ha\b|xa\b|labay\b|roziman\b|mayli\b|xo[‘'\u02bb]p\b|ok\b|okay\b|да\b|yes\b)",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^(yo[‘'\u02bb]q\b|kerak emas\b|shart emas\b|qilmang\b|нет\b|не надо\b|no\b)",
    re.IGNORECASE,
)


def _trim_voice_reply(text: str, limit: int) -> str:
    """Keep a spoken answer short without cutting a sentence in half.

    Evidence from a real call: a 362-character reply became 14 s of audio and
    pushed the queue behind. The trim keeps whole sentences (or a whole word)
    and never changes what the answer says.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    window = text[:limit]
    boundary = max(
        window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("… ")
    )
    if boundary >= limit // 2:
        return window[: boundary + 1].strip()
    cut = window.rsplit(" ", 1)[0].rstrip(",;:-")
    return f"{cut}." if cut else window.strip()


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
        delivery: TelegramDelivery | None = None,
    ) -> None:
        self.settings = settings
        self.started_at = datetime.now(timezone.utc)

        self.tasks = TaskManager()

        reminder_holder: dict[str, ReminderService] = {}

        async def run_reminder(reminder_id: int) -> None:
            service = reminder_holder["service"]
            reminder = await to_thread.run_sync(service.get, reminder_id)
            # A "call me at 18:45" reminder dials instead of sending text.
            if reminder is not None and reminder.kind == "call":
                await self._run_scheduled_call(reminder)
                return
            await service.run_due(reminder_id)

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
        self.speech = speech or build_speech_engine(settings)
        #: Sends text, code, tables and files to the owner's own chat only.
        self.delivery = delivery or TelegramDelivery(
            settings,
            self.telegram,
            recorder=lambda **event: record_event(source="delivery", **event),
        )
        #: One live call keeps one conversation per caller, so follow-up
        #: questions have context and two people never share it. It restarts
        #: after a long idle gap, which means a new call.
        self._call_conversations: dict[int, int] = {}
        self._call_conversation_at: dict[int, float] = {}
        #: Text chats are stored per person for the same reason: the owner's
        #: memory must not leak into a stranger's conversation.
        self._chat_conversations: dict[int, int] = {}
        self._chat_conversation_at: dict[int, float] = {}
        #: "May I call you?" offers waiting for an answer, per guest.
        self._call_offers: dict[int, float] = {}

        self.tasks.bind_reminders(self.reminders)

        context = ToolContext(
            settings=settings,
            tasks=self.tasks,
            reminders=self.reminders,
            sleep=self.sleep,
            calls=self.calls,
            delivery=self.delivery,
        )
        registry = build_tool_registry(context)

        self.brain = brain or AgentBrain(settings, registry=registry)
        self.brain.set_tool_registry(registry)
        # The call layer reuses the same chat pipeline as HTTP and Telegram text.
        self.calls.bind_pipeline(
            self.speech, self.handle_call_message, self.handle_scheduled_call
        )

    async def handle_scheduled_call(self, minutes: int) -> str | None:
        """Store "call me in N minutes" from inside a live call.

        Returns the local time it will run, or None when it could not be
        stored. The session says the time out loud only after this returns.
        """
        try:
            minutes = max(1, min(int(minutes), 720))
        except (TypeError, ValueError):
            return None
        when = datetime.now(self.settings.tzinfo) + timedelta(minutes=minutes)
        try:
            reminder = await to_thread.run_sync(
                partial(
                    self.reminders.create,
                    f"Qo‘ng‘iroq ({minutes} daqiqadan keyin)",
                    when,
                    kind="call",
                )
            )
        except Exception:  # noqa: BLE001 - a failed schedule must not kill the call
            logger.exception("Could not schedule a callback")
            return None
        record_event(
            source="telegram_call",
            message="Callback scheduled",
            details=f"reminder #{reminder.id} at {when.isoformat(timespec='minutes')}",
        )
        return when.isoformat(timespec="minutes")

    async def _run_scheduled_call(self, reminder: Any) -> None:
        """Place the call a callback reminder was created for."""
        started, detail = await self.calls.place("scheduled callback")
        if started:
            await to_thread.run_sync(self.reminders.acknowledge, reminder.id)
            record_event(
                source="telegram_call",
                message="Scheduled callback placed",
                details=detail[:300],
            )
            return
        logger.warning("Scheduled callback failed: %s", detail)
        record_event(
            source="telegram_call",
            message="Scheduled callback failed",
            level="WARNING",
            details=detail[:300],
        )
        if getattr(self.telegram, "ready", False):
            await self.telegram.send_message(
                f"Rejalashtirilgan qo‘ng‘iroqni amalga oshira olmadim: {detail}"
            )

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        self.scheduler.start()
        if memory.is_paused():
            self.scheduler.pause()
        self.reminders.restore()
        detail = await self._start_telegram()
        record_event(
            source="runtime",
            message="Runtime started",
            details=f"telegram={detail}, paused={memory.is_paused()}",
        )

    async def _start_telegram(self) -> str:
        """Start Telegram without ever blocking the rest of the agent."""
        try:
            detail = await self.telegram.start()
        except Exception:  # noqa: BLE001 - Telegram must not stop the server
            logger.exception("Telegram startup raised; continuing without Telegram")
            detail = "connection failed: unexpected error (see log)"

        if self.telegram.ready:
            logger.info("Telegram is connected and authorized")
        else:
            logger.warning("Telegram is NOT connected: %s", detail)
        return detail

    async def stop(self) -> None:
        await self.telegram.stop()
        self.scheduler.shutdown()
        record_event(source="runtime", message="Runtime stopped")

    # -- chat ------------------------------------------------------------- #
    async def chat(
        self,
        message: str,
        conversation_id: int | None = None,
        *,
        voice: bool = False,
        guest: bool = False,
    ) -> ChatOutcome:
        """Persist the turn, ask the brain, persist the answer.

        ``voice`` marks a live phone-call turn: the brain then follows the
        short, spoken-style rules instead of writing a long chat answer.
        ``guest`` marks a person who is not the owner: the brain gets the guest
        rules, no tools at all, and the answer is redacted before it leaves.
        """
        if conversation_id is None:
            conversation = await to_thread.run_sync(memory.create_conversation, message)
            history: list[Any] = []
        else:
            conversation = await to_thread.run_sync(
                memory.get_conversation, conversation_id
            )
            if conversation is None:
                raise LookupError(f"Conversation {conversation_id} not found")
            # A call keeps a shorter history than chat: every extra message
            # delays the next spoken sentence.
            context_limit = (
                self.settings.call_context_messages
                if voice
                else self.settings.chat_context_messages
            )
            history = await to_thread.run_sync(
                memory.recent_messages,
                conversation.id,
                context_limit,
            )

        await to_thread.run_sync(memory.add_message, conversation.id, "user", message)

        try:
            result = await self.brain.reply(
                history=[{"role": item.role, "content": item.content} for item in history],
                user_message=message,
                voice_mode=voice,
                guest=guest,
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
    async def handle_telegram_message(
        self, text: str, user_id: int | None = None
    ) -> str | None:
        """Handle a private message: chat, acknowledge checks, confirm actions.

        ``user_id`` is the numeric id Telegram reports for the sender, never a
        claim made inside the message. It decides whose conversation this is
        and which rules apply: the owner keeps every capability, a guest gets a
        plain chat with no tools and no access to the owner's data.
        """
        role = self._role_of(user_id)
        if role is None:
            # The account layer already dropped this message; the check stays
            # here so no other caller can reach the brain without a role.
            logger.info("Ignoring a Telegram message from an unauthorized sender")
            return None
        if role == "guest":
            return await self._handle_guest_message(text, int(user_id))

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

        return await self._reply_to_user(text, user_id, guest=False)

    async def _handle_guest_message(self, text: str, user_id: int) -> str | None:
        """Answer someone who is not the owner.

        Two rules shape this path. The guest never reaches the owner's tools or
        data, and Ksenus never dials a person who has not agreed to a call in
        this very chat — the offer is asked for, and it expires.
        """
        now = time.monotonic()
        offered_at = self._call_offers.get(user_id)
        if offered_at is not None and now - offered_at > _CALL_OFFER_SECONDS:
            self._call_offers.pop(user_id, None)
            offered_at = None

        if offered_at is not None:
            answer = text.strip()
            if _YES_RE.match(answer):
                self._call_offers.pop(user_id, None)
                self.calls.grant_call_consent(user_id)
                started, detail = await self.calls.place(
                    "the guest agreed to a call", peer_id=user_id
                )
                if started:
                    return "Xo‘p, qo‘ng‘iroq qilayapman."
                # The reason can name a missing setting. That belongs in the
                # owner's log, not in a stranger's chat.
                logger.warning("Guest call to %s failed: %s", user_id, detail)
                self.calls.revoke_call_consent(user_id)
                return "Uzr, hozir qo‘ng‘iroq qilib bo‘lmadi."
            if _NO_RE.match(answer):
                self._call_offers.pop(user_id, None)
                self.calls.revoke_call_consent(user_id)
                return "Xo‘p, qo‘ng‘iroq qilmayman."

        if _CALL_REQUEST_RE.search(text):
            self._call_offers[user_id] = now
            return (
                "Sizga qo‘ng‘iroq qilishim mumkinmi? Rozilik uchun “ha” deb "
                "yozing, qo‘ng‘iroq qilaman."
            )

        return await self._reply_to_user(text, user_id, guest=True)

    async def _reply_to_user(
        self, text: str, user_id: int | None, *, guest: bool
    ) -> str | None:
        """Chat inside that person's own conversation, remembering it briefly.

        Each sender id maps to its own conversation row, so nobody reads
        anybody else's history. A conversation older than the idle window is
        dropped and a fresh one starts.
        """
        key = int(user_id) if user_id is not None else 0
        now = time.monotonic()
        conversation_id = self._chat_conversations.get(key)
        if conversation_id is not None:
            age = now - self._chat_conversation_at.get(key, 0.0)
            if age > _CHAT_CONVERSATION_IDLE_SECONDS:
                conversation_id = None

        try:
            outcome = await self.chat(text, conversation_id, guest=guest)
        except BrainError as exc:
            return exc.public_message
        except LookupError:
            # The stored row is gone: forget the key instead of failing again.
            self._chat_conversations.pop(key, None)
            self._chat_conversation_at.pop(key, None)
            return "I could not find that conversation."

        self._chat_conversations[key] = outcome.conversation_id
        self._chat_conversation_at[key] = time.monotonic()

        if outcome.confirmation_id and not guest:
            return (
                f"{outcome.reply}\n\nThis needs your confirmation. Reply 'confirm' "
                "here or approve it in the dashboard."
            )
        return outcome.reply

    def _role_of(self, user_id: Any) -> str | None:
        """``"owner"``, ``"guest"`` or ``None`` for a Telegram sender id.

        The account object owns the real rule (owner id, Saved Messages,
        allowed guests, public chat). Transports that do not implement it —
        an older test double, for example — fall back to the settings, so the
        answer is still conservative: an unknown id is not the owner.
        """
        if user_id is None:
            return None
        resolver = getattr(self.telegram, "sender_role", None)
        if callable(resolver):
            return resolver(user_id)
        try:
            person = int(user_id)
        except (TypeError, ValueError):
            return None
        if person == self.settings.telegram_owner_user_id:
            return "owner"
        if person in self.settings.chat_user_allowed_ids:
            return "guest"
        return "guest" if self.settings.telegram_public_chat_enabled else None

    async def handle_call_message(
        self, text: str, peer_id: int | None = None
    ) -> str | None:
        """Answer inside a live call: spoken style, short and direct.

        ``peer_id`` keeps one conversation per caller, so two people on two
        calls never hear each other's context. A caller who is not the owner
        gets the guest rules. A failed turn returns ``None`` (silence) instead
        of a canned sentence: repeating "the model returned an empty reply"
        over the phone is worse than saying nothing, and the reason is logged.
        """
        key = int(peer_id) if peer_id is not None else 0
        now = time.monotonic()
        conversation_id = self._call_conversations.get(key)
        if conversation_id is not None:
            age = now - self._call_conversation_at.get(key, 0.0)
            if age > _CALL_CONVERSATION_IDLE_SECONDS:
                conversation_id = None
        # No reported peer means an older caller of this method (HTTP tests and
        # the owner's own dial-out path); a real unknown caller is a guest.
        guest = peer_id is not None and self._role_of(peer_id) != "owner"
        try:
            outcome = await self.chat(text, conversation_id, voice=True, guest=guest)
        except BrainError as exc:
            # Public messages are safe (no prompts, no keys) and useful in logs.
            logger.warning(
                "Telegram call: turn skipped (%s: %s)",
                type(exc).__name__,
                exc.public_message,
            )
            return None
        except LookupError:
            logger.warning("Telegram call: conversation lookup failed; starting over")
            self._call_conversations.pop(key, None)
            return None
        self._call_conversations[key] = outcome.conversation_id
        self._call_conversation_at[key] = time.monotonic()
        reply = _trim_voice_reply(outcome.reply, self.settings.voice_reply_max_chars)
        if len(reply) < len(outcome.reply):
            logger.info(
                "Telegram call: reply trimmed for speech (%d -> %d chars)",
                len(outcome.reply),
                len(reply),
            )
        return reply

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
