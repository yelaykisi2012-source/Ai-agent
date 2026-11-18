"""Telegram user account connection.

Ksenus logs in as a real user account, not a bot. The client is created only
when enabled and configured, connects in the background, answers the
authorized owner only, and never exposes session data through the API.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, int | None], Awaitable[str | None]]
CallHandler = Callable[[object], Awaitable[None]]


async def maybe_await(value: Any) -> Any:
    """Await ``value`` only when it is awaitable.

    Telethon is inconsistent across versions: ``is_connected`` is a plain
    synchronous method, and ``disconnect`` returns a coroutine only while an
    event loop is already running. Awaiting either unconditionally raises
    ``TypeError``, so both calling conventions are supported here.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class TelegramAccount:
    """Owns the Telethon client lifecycle."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        on_message: MessageHandler | None = None,
        on_call: CallHandler | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._on_message = on_message
        self._on_call = on_call
        self._connected = False
        self._authorized = False
        self._detail = "not started"
        self._owner_id: int | None = settings.telegram_owner_user_id
        self._self_id: int | None = None

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> str:
        """Connect and verify authorization. Returns a safe status detail.

        Never raises: a Telegram outage or a broken session must not stop the
        agent itself from starting.
        """
        if not self._settings.telegram_enabled:
            self._detail = "Telegram is disabled (TELEGRAM_ENABLED=false)."
            return self._detail
        if not self._settings.telegram_configured:
            self._detail = "Telegram API credentials are missing."
            return self._detail

        try:
            if self._client is None:
                try:
                    from telethon import TelegramClient
                except ImportError:
                    self._detail = "Telethon is not installed."
                    return self._detail
                self._client = TelegramClient(
                    str(self._settings.telegram_session_file),
                    self._settings.telegram_api_id,
                    self._settings.telegram_api_hash.get_secret_value()
                    if self._settings.telegram_api_hash
                    else "",
                    device_model="KSENUS AI",
                    system_version="Windows 11",
                )

            await self._client.connect()
            # Telethon's ``is_connected()`` is synchronous, so it must not be
            # awaited directly (that raised "'bool' object can't be awaited").
            self._connected = bool(await maybe_await(self._client.is_connected()))
            self._authorized = bool(await self._client.is_user_authorized())
            if not self._authorized:
                self._detail = (
                    "Not authorized. Run: .\\.venv\\Scripts\\python.exe -m "
                    "telegram.authentication login"
                )
                logger.warning("Telegram session is not authorized yet")
                return self._detail

            me = await self._client.get_me()
            self._self_id = getattr(me, "id", None) if me is not None else None
            if self._owner_id is None and me is not None:
                self._owner_id = getattr(me, "id", None)
                logger.info("Telegram owner id resolved from the logged-in account")
            self._register_handlers()
            self._detail = "connected"
            logger.info("Telegram user account connected")
        except Exception as exc:  # noqa: BLE001 - network/login failures are expected
            self._detail = f"connection failed: {type(exc).__name__}"
            self._connected = False
            self._authorized = False
            # Full traceback for diagnosis. It never contains the api hash,
            # phone number, login code or session data.
            logger.error(
                "Telegram connection failed: %s (session_present=%s)",
                type(exc).__name__,
                self._session_present(),
                exc_info=True,
            )
        return self._detail

    async def stop(self) -> None:
        if self._client is not None:
            try:
                # ``disconnect`` is only awaitable while an event loop runs.
                await maybe_await(self._client.disconnect())
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("Telegram disconnect failed", exc_info=True)
        self._connected = False
        self._authorized = False
        self._detail = "disconnected"

    # -- state ------------------------------------------------------------ #
    @property
    def ready(self) -> bool:
        return self._connected and self._authorized

    @property
    def client(self) -> Any | None:
        """The underlying Telethon client (None before start)."""
        return self._client

    @property
    def owner_id(self) -> int | None:
        return self._owner_id

    @property
    def self_id(self) -> int | None:
        """The logged-in account's own Telegram user id."""
        return self._self_id

    def status(self) -> dict[str, Any]:
        """Connection state. Never includes the session string or the owner id."""
        return {
            "enabled": self._settings.telegram_enabled,
            "configured": self._settings.telegram_configured,
            "connected": self._connected,
            "authorized": self._authorized,
            "session_present": self._session_present(),
            "owner_configured": self._owner_id is not None,
            "live_calls_enabled": self._settings.telegram_calls_enabled,
            "detail": self._detail,
        }

    def _session_present(self) -> bool:
        """True when the local Telethon session file exists (path only, no data)."""
        try:
            return self._settings.telegram_session_file.with_suffix(".session").is_file()
        except OSError:  # pragma: no cover - unreadable path is reported as absent
            return False

    # -- messaging -------------------------------------------------------- #
    async def send_document(
        self,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        """Upload a file to the owner. True only when Telethon returned a message."""
        if not self.ready or self._client is None:
            logger.warning("Telegram file not sent: account is not ready")
            return False
        target = user_id or self._owner_id
        if target is None:
            logger.warning("Telegram file not sent: no owner id configured")
            return False
        try:
            import io

            sent = await self._client.send_file(
                target,
                io.BytesIO(data),
                file_name=filename,
                caption=(caption or "")[:1024],
                force_document=True,
            )
            # A real Message object is the only proof Telegram accepted it.
            return sent is not None and getattr(sent, "id", None) is not None
        except Exception:  # noqa: BLE001 - Telegram errors are logged, not raised
            logger.exception("Telegram send_document failed")
            return False

    async def send_message(self, text: str, user_id: int | None = None) -> bool:
        """Send a message to the owner. False when not connected."""
        if not self.ready or self._client is None:
            logger.warning("Telegram message not sent: account is not ready")
            return False
        target = user_id or self._owner_id
        if target is None:
            logger.warning("Telegram message not sent: no owner id configured")
            return False
        try:
            sent = await self._client.send_message(target, text[:4096])
            return sent is not None and getattr(sent, "id", None) is not None
        except Exception:  # noqa: BLE001 - Telegram errors are logged, not raised
            logger.exception("Telegram send_message failed")
            return False

    # -- handlers --------------------------------------------------------- #
    def _register_handlers(self) -> None:
        if self._client is None:
            return
        try:
            from telethon import events
            from telethon.tl import types

            self._client.add_event_handler(self._handle_incoming, events.NewMessage(incoming=True))
            self._client.add_event_handler(
                self._handle_call_update, events.Raw(types.UpdatePhoneCall)
            )
            self._client.add_event_handler(
                self._handle_call_update, events.Raw(types.UpdatePhoneCallSignalingData)
            )
        except Exception:  # noqa: BLE001 - handler wiring must not kill startup
            logger.exception("Could not register Telegram handlers")

    async def _handle_incoming(self, event: Any) -> None:
        """Forward private messages from people who are allowed to talk.

        Trust comes only from the numeric sender id Telegram reports, never
        from what the message claims. Group and channel messages are ignored,
        and so are bots: two automated accounts answering each other would loop
        forever.
        """
        if getattr(event, "is_group", False) or getattr(event, "is_channel", False):
            logger.info("Ignoring a Telegram group/channel message")
            return
        if getattr(getattr(event, "sender", None), "bot", False):
            logger.info("Ignoring a Telegram message from a bot account")
            return
        sender_id = getattr(event, "sender_id", None)
        role = self.sender_role(sender_id)
        if role is None:
            logger.info("Ignoring Telegram message from an unauthorized sender")
            return
        text = (getattr(event, "raw_text", None) or "").strip()
        if not text:
            return
        if self._on_message is None:
            return
        try:
            reply = await self._on_message(text, sender_id)
        except Exception:  # noqa: BLE001 - bad handler must not kill the client
            logger.exception("Telegram message handler failed (%s role)", role)
            reply = "Sorry, something went wrong on my side."
        if reply:
            try:
                await event.reply(reply[:4096])
            except Exception:  # noqa: BLE001
                logger.exception("Could not reply on Telegram")

    def sender_role(self, sender_id: Any) -> str | None:
        """Return ``"owner"``, ``"guest"`` or ``None`` for a Telegram sender id.

        A guest exists only when the id is explicitly allowed or public chat is
        enabled; otherwise the message is ignored exactly as before.
        """
        if sender_id is None:
            return None
        try:
            person = int(sender_id)
        except (TypeError, ValueError):
            return None
        if self._owner_id is not None and person == self._owner_id:
            return "owner"
        if self._self_id is not None and person == self._self_id:
            return "owner"  # the account's own "Saved Messages" chat
        if person in self._settings.chat_user_allowed_ids:
            return "guest"
        if self._settings.telegram_public_chat_enabled:
            return "guest"
        return None

    async def _handle_call_update(self, update: Any) -> None:
        if self._on_call is not None:
            try:
                await self._on_call(update)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram call handler failed")
