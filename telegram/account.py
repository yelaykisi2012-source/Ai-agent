"""Telegram user account connection.

Ksenus logs in as a real user account, not a bot. The client is created only
when enabled and configured, connects in the background, answers the
authorized owner only, and never exposes session data through the API.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str], Awaitable[str | None]]
CallHandler = Callable[[object], Awaitable[None]]


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

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> str:
        """Connect and verify authorization. Returns a safe status detail."""
        if not self._settings.telegram_enabled:
            self._detail = "Telegram is disabled (TELEGRAM_ENABLED=false)."
            return self._detail
        if not self._settings.telegram_configured:
            self._detail = "Telegram API credentials are missing."
            return self._detail

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

        try:
            await self._client.connect()
            self._connected = bool(await self._client.is_connected())
            self._authorized = bool(await self._client.is_user_authorized())
            if not self._authorized:
                self._detail = (
                    "Not authorized. Run: .\\.venv\\Scripts\\python.exe -m "
                    "telegram.authentication login"
                )
                logger.warning("Telegram session is not authorized yet")
                return self._detail

            me = await self._client.get_me()
            if self._owner_id is None and me is not None:
                self._owner_id = getattr(me, "id", None)
                logger.info("Telegram owner id resolved from the logged-in account")
            self._register_handlers()
            self._detail = "connected"
            logger.info("Telegram user account connected")
        except Exception as exc:  # noqa: BLE001 - network/login failures are expected
            self._detail = f"connection failed: {type(exc).__name__}"
            self._connected = False
            logger.error("Telegram connection failed: %s", type(exc).__name__)
        return self._detail

    async def stop(self) -> None:
        if self._client is not None and self._connected:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("Telegram disconnect failed", exc_info=True)
        self._connected = False
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

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.telegram_enabled,
            "configured": self._settings.telegram_configured,
            "connected": self._connected,
            "authorized": self._authorized,
            "owner_configured": self._owner_id is not None,
            "live_calls_enabled": self._settings.telegram_calls_enabled,
            "detail": self._detail,
        }

    # -- messaging -------------------------------------------------------- #
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
            await self._client.send_message(target, text[:4096])
            return True
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
        except Exception:  # noqa: BLE001 - handler wiring must not kill startup
            logger.exception("Could not register Telegram handlers")

    async def _handle_incoming(self, event: Any) -> None:
        """Forward messages from the authorized owner only."""
        sender_id = getattr(event, "sender_id", None)
        if self._owner_id is None or sender_id != self._owner_id:
            logger.info("Ignoring Telegram message from unauthorized sender")
            return
        text = (getattr(event, "raw_text", None) or "").strip()
        if not text:
            return
        if self._on_message is None:
            return
        try:
            reply = await self._on_message(text)
        except Exception:  # noqa: BLE001 - bad handler must not kill the client
            logger.exception("Telegram message handler failed")
            reply = "Sorry, something went wrong on my side."
        if reply:
            try:
                await event.reply(reply[:4096])
            except Exception:  # noqa: BLE001
                logger.exception("Could not reply on Telegram")

    async def _handle_call_update(self, update: Any) -> None:
        if self._on_call is not None:
            try:
                await self._on_call(update)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram call handler failed")
