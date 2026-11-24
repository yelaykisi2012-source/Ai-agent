"""Application runtime.

One object owns the long-lived services, so FastAPI routes stay thin and the
Telegram handler can reuse exactly the same chat path as the HTTP API. Nothing
here fabricates a capability: each service reports whether it is actually able
to do its job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from anyio import to_thread

import memory
import messenger_inbox
import owner_profile
import updates
from brain import AgentBrain, BrainError, ToolResult
from config import PROJECT_ROOT, Settings
from database import record_event
from tasks.manager import TaskManager
from tasks.reminders import ReminderService
from tasks.scheduler import AgentScheduler
from tasks.sleep_monitor import SleepMonitor
from computer import Computer
from gui_control import GuiController
from gui_cursor import CursorOverlay
from integrations.apyhub import BackgroundRemover
from local_files import LocalFiles
from runs import AgentRunService
from telegram.account import TelegramAccount
from telegram.calls import CallManager
from telegram.group_call import GroupCallManager
from computer_mode import ComputerMode
from companion import KsenusCompanion, SpeechEvents
from telegram.delivery import TelegramDelivery
from tools import ToolContext, build_tool_registry
from web import Web
from voice.edge_speech import EdgeSpeechEngine
from voice.pc_speaker import PcSpeaker
from voice.speech_engine import SpeechEngine, build_speech_engine

logger = logging.getLogger(__name__)

#: A call conversation older than this is treated as a different call.
_CALL_CONVERSATION_IDLE_SECONDS = 600.0
#: Words that approve or decline a pending action out loud in a call. Computer
#: mode is voice-only, so "ha" has to work where typing "confirm" cannot.
_VOICE_YES = frozenset(
    {
        "ha",
        "haf",
        "xop",
        "xo‘p",
        "xo'p",
        "mayli",
        "bajar",
        "bajaring",
        "bajaramiz",
        "bo‘ladi",
        "bo'ladi",
        "boladi",
        "ruxsat",
        "tasdiq",
        "tasdiqlayman",
        "davom",
    }
)
_VOICE_NO = frozenset(
    {"yoq", "yo‘q", "yo'q", "bekor", "kerakmas", "kerak-emas", "to‘xta"}
)
#: A spoken yes only applies to a fresh request, never to yesterday's queue.
_VOICE_CONFIRM_FRESH_SECONDS = 600.0
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
#: A half-finished goal older than this is not resumed any more.
_RESUME_MAX_AGE_SECONDS = 24 * 3600


def _resume_note(stored: str | None, message: str) -> str | None:
    """Tell the model about a task that never reached an answer.

    ``stored`` is ``"<iso timestamp>|<text>"`` written at the start of the
    turn that failed. A fresh goal, the same request again, or an old one
    needs no note.
    """
    if not stored or "|" not in stored:
        return None
    stamp, _, goal = stored.partition("|")
    goal = goal.strip()
    if not goal or goal == (message or "").strip():
        return None
    try:
        created = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age > _RESUME_MAX_AGE_SECONDS:
        return None
    return (
        f"(Eslatma: oldingi vazifa tugallanmay qolgan edi — «{goal[:300]}». "
        "Agar egasi hozir shu haqida gapirsa, avval o‘sha ishni oxiriga "
        "yetkaz, keyin yangisiga o‘t.)"
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

        #: Every turn the agent takes is recorded, whatever the channel.
        self.runs = AgentRunService(settings)
        root = settings.local_files_root.strip()
        self.files = LocalFiles(
            Path(root).expanduser() if root else PROJECT_ROOT,
            max_bytes=settings.local_files_max_kb * 1024,
        )
        #: Shell + file control for the computer channel. None when disabled.
        self.computer = (
            Computer(
                self.files,
                enabled=settings.computer_enabled,
                confirm_commands=(
                    settings.computer_confirm_commands
                    and not settings.owner_auto_approve
                ),
                default_timeout=settings.computer_command_timeout,
                max_output_kb=settings.computer_output_max_kb,
            )
            if settings.computer_enabled
            else None
        )

        #: Public web search and page reading. None when WEB_ENABLED=false.
        self.web = (
            Web(
                timeout=settings.web_timeout_seconds,
                max_results=settings.web_max_results,
                max_bytes=settings.web_fetch_max_kb * 1024,
                exa_api_key=settings.exa_key,
            )
            if settings.web_enabled
            else None
        )

        #: ApyHub background removal. Without APY_TOKEN the tool says so and
        #: sends nothing; the token stays on the server.
        self.images = BackgroundRemover(token=settings.apy_token_value)

        self.tasks = TaskManager()

        reminder_holder: dict[str, ReminderService] = {}

        async def run_reminder(reminder_id: int) -> None:
            service = reminder_holder["service"]
            reminder = await to_thread.run_sync(service.get, reminder_id)
            # A "call me at 18:45" reminder dials instead of sending text.
            if reminder is not None and reminder.kind == "call":
                if not getattr(self.telegram, "ready", False):
                    # No Telegram account: ring inside the messenger window
                    # instead of failing silently. The window shows the call.
                    messenger_inbox.push(
                        reminder.message, kind="call", reminder_id=reminder_id
                    )
                    await to_thread.run_sync(service.acknowledge, reminder_id)
                    record_event(
                        source="reminder",
                        message=f"Call reminder #{reminder_id} rang in the messenger",
                        details="telegram is not connected",
                    )
                    return
                await self._run_scheduled_call(reminder)
                return
            await service.run_due(reminder_id)

        self.scheduler = AgentScheduler(settings, run_reminder)

        account_holder: dict[str, TelegramAccount] = {}

        async def notify(text: str, reminder_id: int | None = None) -> bool:
            # Local first: the messenger window is always here, Telegram often
            # is not. Reaching the window acknowledges the reminder server-side.
            local = messenger_inbox.push(text, reminder_id=reminder_id) is not None
            account = account_holder.get("account")
            telegram = bool(account and account.ready and await account.send_message(text))
            delivered = local or telegram
            record_event(
                source="notifier",
                message="Notification delivered" if delivered else "Notification not delivered",
                level="INFO" if delivered else "WARNING",
                details=f"messenger={local}, telegram={telegram}: {text[:400]}",
            )
            return delivered

        async def compose_reminder(reminder: Any) -> str | None:
            """Let the agent decide what a due reminder should say."""
            prompt = (
                "Ichki eslatma vaqti keldi. Foydalanuvchiga yuboriladigan aniq "
                "matnni yoz.\n"
                f"Turi: {reminder.kind}\n"
                f"Eslatma: {reminder.message}\n\n"
                "Qoidalar: 1-2 gap, o‘zbek tilida, «siz» deb murojaat qil. Faqat "
                "yuboriladigan matnni yoz, izoh qo‘shma. Agar eslatish hozir "
                "ma’nosiz bo‘lsa (masalan, ish allaqachon bajarilgan), faqat SKIP "
                "deb yoz.\n"
                "Bu turni faqat matn yozish uchun ishlat: tool chaqirma."
            )
            run_id = await to_thread.run_sync(
                self.runs.start, "reminder", "telegram", prompt
            )
            try:
                result = await self.brain.reply(
                    history=[], user_message=prompt, use_tools=False
                )
            except BrainError as exc:
                await to_thread.run_sync(
                    partial(self.runs.finish, run_id, status="failed", error=exc.public_message)
                )
                raise
            reply = (result.text or "").strip()
            skipped = not reply or reply.upper().startswith("SKIP")
            await to_thread.run_sync(
                partial(
                    self.runs.finish,
                    run_id,
                    status="skipped" if skipped else "ok",
                    reply=reply,
                )
            )
            return None if skipped else reply

        self.reminders = ReminderService(settings, self.scheduler, notify)
        self.reminders.set_composer(compose_reminder)
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
        #: Voice chats inside groups: join muted, listen, leave on request.
        self.group_calls = GroupCallManager(settings, self.telegram)
        #: Kompyuter rejimi: only the small agent on this computer. No Telegram
        #: call and no extra panel: the agent itself does the talking.
        self.computer_mode = ComputerMode(settings)
        #: Real mouse/keyboard on this screen, with the animated cursor layer.
        window_url = (
            os.environ.get("KSENUS_WINDOW_URL", "").strip()
            or f"http://127.0.0.1:{settings.api_port}"
        )
        #: Kept here as well, because computer mode hides the cursor halo and
        #: leaves the small desktop agent as the only thing on screen.
        self.cursor_overlay = CursorOverlay(
            enabled=settings.gui_control_enabled,
            base_url=window_url,
            idle_seconds=settings.gui_cursor_idle_seconds,
            token=settings.api_token_value,
        )
        self.gui = GuiController(
            enabled=settings.gui_control_enabled,
            overlay=self.cursor_overlay,
        )
        #: The small animated Ksenus on this computer's screen. It belongs to
        #: computer mode and can also be shown by hand over the local API.
        self.companion = KsenusCompanion(
            enabled=settings.companion_enabled,
            base_url=window_url,
            scale=settings.companion_scale,
            visible_when=lambda: self.computer_mode.active,
            idle_sleep_seconds=settings.companion_sleep_seconds,
            token=settings.api_token_value,
        )
        #: Watches this computer's own windows (program, hours, last title) so
        #: the robot remembers what the owner does, not just what they type.
        self.profile_observer = owner_profile.OwnerObserver(
            enabled=settings.owner_profile_enabled
        )
        self.computer_mode.on_active = self._on_computer_mode
        # The speech engine drives the pet's speaking and listening faces.
        engine = speech or build_speech_engine(settings)
        self.speech = SpeechEvents(engine, self.companion)
        #: In computer mode the answers are read out on this computer's
        #: speakers; there is no call to carry the voice any more. The keyless
        #: Edge voice stands in when the configured provider is slow or down.
        fallback = None
        if settings.tts_backend != "edge":
            candidate = EdgeSpeechEngine(settings)
            if candidate.configured:
                fallback = candidate
        self.pc_speaker = PcSpeaker(
            engine,
            enabled=settings.companion_voice_enabled,
            fallback=fallback,
            on_speaking=self._on_pc_speaking,
        )
        #: The answer currently being read out, so a new one can replace it.
        self._speech_task: asyncio.Task[Any] | None = None
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
        #: The global hotkey listener, when one is running.
        self.hotkey: Any | None = None
        #: "May I call you?" offers waiting for an answer, per guest.
        self._call_offers: dict[int, float] = {}
        #: Consecutive failed turns per caller. One failure stays silent (a
        #: repeated error message is worse than a pause); two in a row mean the
        #: person is talking into a dead line, and one short honest sentence is
        #: better than more silence.
        self._call_failures: dict[int, int] = {}

        self.tasks.bind_reminders(self.reminders)

        context = ToolContext(
            settings=settings,
            tasks=self.tasks,
            reminders=self.reminders,
            sleep=self.sleep,
            calls=self.calls,
            delivery=self.delivery,
            files=self.files if settings.local_files_enabled else None,
            runs=self.runs,
            computer=self.computer,
            web=self.web,
            images=self.images,
            telegram=self.telegram,
            group_calls=self.group_calls,
            computer_mode=self.computer_mode,
            gui=self.gui,
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
        # The storage layer already reports a failed dial; do not say it twice.
        started, detail = await self.calls.place("scheduled callback", notify=False)
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
        notice = f"Rejalashtirilgan qo‘ng‘iroqni amalga oshira olmadim: {detail}"
        if "UserPrivacyRestrictedError" in detail:
            notice += (
                "\n\nSabab: Telegram sizning «qo‘ng‘iroqlar» maxfiylik sozlamangiz "
                "tufayli bu akkauntdan kelgan qo‘ng‘iroqni qabul qilmadi. "
                "Telegram → Settings → Privacy and Security → Calls → Everybody "
                "qilib qo‘ying (yoki Ksenus akkauntini kontaktga qo‘shing), "
                "shundan keyin rejalashtirilgan qo‘ng‘iroq keladi."
            )
        if getattr(self.telegram, "ready", False):
            if not await self.telegram.send_message(notice):
                logger.error("Could not tell the owner that the scheduled call failed")
        else:
            logger.error("Scheduled call failed and Telegram is not ready: %s", detail)

    # -- desktop companion ------------------------------------------------- #
    def _on_computer_mode(self, active: bool) -> None:
        """Computer mode shows one thing only: the small desktop agent.

        The cursor halo, the mode panel and anything else the agent draws are
        put away, so the owner sees the little robot and nothing else. When the
        mode ends the halo comes back to its setting and a sentence that is
        still being read out stops.
        """
        if active:
            self._set_cursor_overlay_enabled(False)
            self.companion.show("kompyuter rejimi")
            self.companion.event("computer_mode_started")
        else:
            task = self._speech_task
            self._speech_task = None
            if task is not None and not task.done():
                task.cancel()
            self.pc_speaker.stop()
            self.companion.event("computer_mode_stopped")
            self.companion.hide("kompyuter rejimi tugadi")
            self._set_cursor_overlay_enabled(self.settings.gui_control_enabled)

    def _on_pc_speaking(self, speaking: bool) -> None:
        """The desktop voice started or stopped: keep the pet's mouth honest."""
        self.computer_mode.set_speaking(speaking)
        self.companion.event("speech_started" if speaking else "speech_finished")

    def _set_cursor_overlay_enabled(self, enabled: bool) -> None:
        """Turn the cursor halo on or off without touching the GUI controller."""
        overlay = getattr(self, "cursor_overlay", None)
        if overlay is None:
            return
        overlay.enabled = bool(enabled)
        if not enabled:
            overlay.close()

    # -- lifecycle -------------------------------------------------------- #
    async def start(self, *, connect_telegram: bool = True) -> None:
        # Telegram first: a callback that is already due must not be attempted
        # while the account is still connecting, or it fails for nothing.
        # The desktop channel passes False: the agent then runs on this computer
        # without touching the Telegram session the server already holds.
        if connect_telegram:
            detail = await self._start_telegram()
        else:
            detail = "not started (desktop channel)"
            logger.info("Telegram not started: desktop-only session")
        self.scheduler.start()
        self.profile_observer.start()
        # One Telegram message per new version: "yuklab olin, iltimos".
        await self._announce_update()
        if memory.is_paused():
            self.scheduler.pause()
        self.reminders.restore()
        # A global hotkey opens a one-line prompt without opening Telegram.
        self.hotkey = None
        if self.settings.hotkey_enabled:
            from hotkey import start_hotkey

            self.hotkey = start_hotkey(self.settings.hotkey)
        record_event(
            source="runtime",
            message="Runtime started",
            details=(
                f"telegram={detail}, paused={memory.is_paused()}, "
                f"hotkey={getattr(self.hotkey, 'hotkey', 'off')}"
            ),
        )

    async def _announce_update(self) -> None:
        """Tell the owner a newer Ksenos exists, once per version.

        The messenger window always shows it; Telegram also gets the message
        with the download button when the account is connected. Nothing is
        announced when no newer release was published, and a restart never
        repeats a notice the owner already saw.
        """
        try:
            payload = updates.payload()
        except Exception:  # noqa: BLE001 - an update check must never stop startup
            logger.debug("Update check failed", exc_info=True)
            return
        if not payload.get("available"):
            return
        latest = str(payload.get("latest") or "")
        if not latest or not updates.needs_announce(latest):
            return

        text = updates.announce_text(payload)
        local = messenger_inbox.push(text, kind="update") is not None
        sent = False
        if getattr(self.telegram, "ready", False):
            sender = getattr(self.telegram, "send_update_notice", None)
            try:
                if sender is not None:
                    sent = bool(
                        await sender(text, url=updates.download_url(self.settings))
                    )
                else:  # pragma: no cover - every real account has the method
                    sent = bool(await self.telegram.send_message(text))
            except Exception:  # noqa: BLE001 - a notice must not break startup
                logger.exception("Update notice could not be sent to Telegram")
        if sent or local:
            updates.mark_announced(latest)
            record_event(
                source="updates",
                message="Update notice delivered",
                details=f"version={latest}, telegram={sent}, messenger={local}",
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
        if self.hotkey is not None:
            self.hotkey.stop()
            self.hotkey = None
        await self.telegram.stop()
        self.gui.close()
        self.companion.close()
        self.profile_observer.stop()
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
        trigger: str = "chat",
        channel: str = "http",
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatOutcome:
        """Persist the turn, ask the brain, persist the answer.

        ``progress`` receives one short "what I am doing" line per tool call,
        so a long task does not look like silence.
        ``voice`` marks a live phone-call turn: the brain then follows the
        short, spoken-style rules instead of writing a long chat answer.
        ``guest`` marks a person who is not the owner: the brain gets the guest
        rules, no tools at all, and the answer is redacted before it leaves.
        """
        run_id = await to_thread.run_sync(self.runs.start, trigger, channel, message)
        self.companion.event("user_message_received")

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
        if not guest and not voice:
            # «eslab qo‘y: …» is kept without being asked again; nothing else
            # is saved automatically, so the memory stays the owner's own.
            saved = await to_thread.run_sync(memory.auto_remember, message)
            if saved is not None:
                await to_thread.run_sync(
                    lambda: record_event(
                        source="memory",
                        message="Remembered an explicit fact",
                        details=saved.content[:200],
                    )
                )
        # The owner's own requests and stated likes are part of the profile the
        # next turn is answered from. Guests are never observed.
        if not guest:
            await to_thread.run_sync(owner_profile.observe_request, message)

        # An unfinished earlier task is remembered here, so a turn that died
        # half-way is picked up instead of being forgotten. The note goes to
        # the model only; the stored message stays exactly what was said.
        goal_key = f"goal:{conversation.id}"
        prompt = message
        if not voice:
            pending_goal = await to_thread.run_sync(memory.get_state, goal_key)
            note = _resume_note(pending_goal, message)
            if note:
                prompt = f"{message}\n\n{note}"
        stamp = datetime.now(timezone.utc).isoformat()
        await to_thread.run_sync(memory.set_state, goal_key, f"{stamp}|{message}")

        self.companion.event("ai_thinking_started")
        try:
            result = await self.brain.reply(
                history=[{"role": item.role, "content": item.content} for item in history],
                user_message=prompt,
                voice_mode=voice,
                guest=guest,
                progress=progress,
            )
        except BrainError as exc:
            self.companion.event("error_occurred")
            await to_thread.run_sync(
                partial(
                    self.runs.finish,
                    run_id,
                    status="failed",
                    error=exc.public_message,
                )
            )
            await to_thread.run_sync(
                lambda: record_event(
                    source="chat",
                    message="Chat request failed",
                    level="ERROR",
                    details=f"conversation_id={conversation.id}, reason={exc.public_message}",
                )
            )
            raise

        self.companion.event("ai_response_ready")
        await to_thread.run_sync(
            memory.add_message, conversation.id, "assistant", result.text
        )
        await to_thread.run_sync(
            partial(
                self.runs.finish,
                run_id,
                status="ok",
                reply=result.text,
                tools=len(result.tool_results),
            )
        )
        # The task reached an answer, so there is nothing to resume.
        await to_thread.run_sync(memory.set_state, goal_key, "")

        outcome = ChatOutcome(
            conversation_id=conversation.id,
            reply=result.text,
            model=result.model,
            usage=result.usage,
            tools=result.tool_results,
        )

        if self.computer_mode.active:
            # No Telegram call in this mode: the small agent is the voice.
            previous = self._speech_task
            if previous is not None and not previous.done():
                previous.cancel()
            self._speech_task = asyncio.create_task(self.pc_speaker.speak(result.text))

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

    def _telegram_progress(self) -> Callable[[str], Awaitable[None]] | None:
        """Send progress lines to the owner in Telegram, when it is connected."""
        account = self.telegram
        if account is None or not getattr(account, "ready", False):
            return None

        async def send(text: str) -> None:
            await account.send_progress(text)

        return send

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

        progress = None if guest else self._telegram_progress()
        try:
            outcome = await self.chat(
                text,
                conversation_id,
                guest=guest,
                trigger="telegram_message",
                channel="telegram",
                progress=progress,
            )
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
        if person in self.settings.owner_user_ids:
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
        if not guest:
            approved = await self._voice_confirmation(text)
            if approved is not None:
                return approved
        try:
            outcome = await self.chat(
                text, conversation_id, voice=True, guest=guest, trigger="voice", channel="call"
            )
        except BrainError as exc:
            # Public messages are safe (no prompts, no keys) and useful in logs.
            logger.warning(
                "Telegram call: turn skipped (%s: %s)",
                type(exc).__name__,
                exc.public_message,
            )
            failures = self._call_failures.get(key, 0) + 1
            self._call_failures[key] = failures
            if failures >= 2:
                return (
                    "Kechirasiz, hozir javob bera olmayapman. "
                    "Biroz kutib, yana bir marta ayting."
                )
            return None
        except LookupError:
            logger.warning("Telegram call: conversation lookup failed; starting over")
            self._call_conversations.pop(key, None)
            return None
        self._call_conversations[key] = outcome.conversation_id
        self._call_conversation_at[key] = time.monotonic()
        self._call_failures.pop(key, None)
        reply = _trim_voice_reply(outcome.reply, self.settings.voice_reply_max_chars)
        if len(reply) < len(outcome.reply):
            logger.info(
                "Telegram call: reply trimmed for speech (%d -> %d chars)",
                len(outcome.reply),
                len(reply),
            )
        return reply

    async def _voice_confirmation(self, text: str) -> str | None:
        """Approve or decline the newest pending action by voice.

        Returns the spoken answer when the utterance was an answer to a waiting
        confirmation, and ``None`` when it is an ordinary sentence (so the
        normal turn runs). Only the newest action is touched, only while it is
        fresh, and it is consumed exactly once.
        """
        words = set(re.findall(r"[a-z‘'ʻ-]+", (text or "").lower()))
        yes = words & _VOICE_YES
        no = words & _VOICE_NO
        if not yes and not no:
            return None
        action = await to_thread.run_sync(memory.latest_pending_action)
        if action is None:
            return None
        created = action.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age > _VOICE_CONFIRM_FRESH_SECONDS:
            logger.info(
                "Telegram call: a spoken answer was ignored; the pending action "
                "is %d s old",
                int(age),
            )
            return None
        if no and not yes:
            await to_thread.run_sync(memory.consume_pending_action, action.id)
            logger.info("Telegram call: the pending action was declined by voice")
            return "Xo‘p, bekor qildim."
        result = await self.execute_confirmation(action.id)
        if result is None:
            return "Bu amal endi mavjud emas."
        ok = getattr(result, "status", "") == "ok"
        logger.info(
            "Telegram call: a pending action was confirmed by voice (%s)",
            getattr(result, "name", action.tool),
        )
        return "Bajardim." if ok else "Bajara olmadim."

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
            "computer_voice": self.pc_speaker.available,
            "image_background_removal": self.images.configured,
            "dashboard": self.settings.dashboard_enabled,
        }
