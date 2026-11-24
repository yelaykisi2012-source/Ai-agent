"""Telegram user account connection.

Ksenus logs in as a real user account, not a bot. The client is created only
when enabled and configured, connects in the background, answers the
authorized owner only, and never exposes session data through the API.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, int | None], Awaitable[str | None]]
CallHandler = Callable[[object], Awaitable[None]]

#: Every URL in a channel post, so a video the channel linked is not lost.
_URL = re.compile(r"https?://[^\s<>\"'«»]+")


def _post_links(text: str) -> list[str]:
    """Links written inside a post, without duplicates or trailing punctuation."""
    found: list[str] = []
    for match in _URL.findall(text or ""):
        link = match.rstrip(".,;:!?)»\"'")
        if link and link not in found:
            found.append(link)
    return found[:5]


def _post_row(message: Any, username: str) -> str | None:
    """One readable line for a channel post: date, media, link and snippet."""
    text = (getattr(message, "message", None) or "").strip()
    media = _media_kind(message)
    links = _post_links(text)
    if not text and not media and not links:
        return None
    post_id = getattr(message, "id", None)
    date = getattr(message, "date", None)
    stamp = date.strftime("%d.%m %H:%M") if hasattr(date, "strftime") else ""
    post_link = f"https://t.me/{username}/{post_id}" if post_id else ""
    parts = [item for item in (stamp, media) if item]
    head = " | ".join(parts)
    snippet = " ".join(text.split())[:180]
    row = f"- {head + ' | ' if head else ''}{post_link}"
    if links:
        row += " | havolalar: " + ", ".join(links)
    if snippet:
        row += f" | «{snippet}»"
    return row


def _media_kind(message: Any) -> str:
    """What kind of media a post carries, in one word, or an empty string."""
    for name in ("video", "video_note", "animation", "photo", "document", "audio"):
        if getattr(message, name, None):
            return name
    return ""


def _parse_group_target(raw: str) -> tuple[str, str]:
    """Turn an ``@username`` or ``t.me`` link into ``(kind, value)``.

    ``kind`` is ``"username"`` or ``"invite"``. Anything else (a phone number,
    a chat id, a bare path) is refused, because guessing could join the wrong
    chat.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("A group @username or t.me invite link is required.")
    text = text.split("?")[0].rstrip("/")
    if text.startswith("@"):
        return "username", text[1:]
    if text.startswith("+"):
        return "invite", text[1:]

    lowered = text.lower()
    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "t.me/",
        "telegram.me/",
    ):
        if not lowered.startswith(prefix):
            continue
        rest = text[len(prefix) :]
        if rest.lower() == "joinchat":
            raise ValueError("That t.me link does not point to a group.")
        if rest.startswith("+") and len(rest) > 1:
            return "invite", rest[1:]
        if rest.lower().startswith("joinchat/") and len(rest) > len("joinchat/"):
            return "invite", rest[len("joinchat/") :]
        if rest and "/" not in rest:
            return "username", rest
        raise ValueError("That t.me link does not point to a group.")

    if "/" not in text and not text.isdigit():
        return "username", text
    raise ValueError(
        "Only a group @username or a t.me invite link can be joined, not a chat id."
    )


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
        #: The account's own @username, used to notice a mention in a group.
        self._self_username: str | None = None

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
            self._self_username = getattr(me, "username", None) if me is not None else None
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

    async def send_progress(self, text: str) -> bool:
        """Send a one-line "what I am doing now" note to the owner.

        It goes to the owner's own chat and never touches the stored
        conversation, so a progress line cannot be mistaken for an answer.
        """
        note = (text or "").strip()
        if not note:
            return False
        return await self.send_message(note)

    # -- groups ----------------------------------------------------------- #
    async def list_groups(self, limit: int = 20) -> list[str]:
        """Groups and channels this account is already in (titles and ids)."""
        if not self.ready or self._client is None:
            return []
        try:
            dialogs = await self._client.get_dialogs(limit=200)
        except Exception:  # noqa: BLE001 - listing is never worth a crash
            logger.exception("Telegram group listing failed")
            return []
        rows: list[str] = []
        for dialog in dialogs or []:
            if not (getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
                continue
            entity = getattr(dialog, "entity", None)
            title = getattr(entity, "title", None) or getattr(dialog, "name", None) or "?"
            username = getattr(entity, "username", None)
            handle = f" @{username}" if username else ""
            rows.append(f"{title}{handle} (id={getattr(dialog, 'id', '?')})")
            if len(rows) >= max(1, limit):
                break
        return rows

    async def join_group(self, target: str) -> tuple[bool, str]:
        """Join a group by ``@username`` or a ``t.me`` invite link.

        Returns ``(joined, detail)``. ``detail`` is the sentence the owner
        reads, so it never contains a session string or an internal id.
        """
        if not self.ready or self._client is None:
            return False, "Telegram is not connected, so nothing was joined."
        try:
            kind, value = _parse_group_target(target)
        except ValueError as exc:
            return False, str(exc)

        try:
            from telethon import functions
            from telethon.errors import (
                ChannelsTooMuchError,
                ChannelInvalidError,
                ChannelPrivateError,
                InviteHashExpiredError,
                InviteHashInvalidError,
                UserAlreadyParticipantError,
            )
        except ImportError:  # pragma: no cover - Telethon is a dependency
            return False, "Telethon is not installed, so groups cannot be joined."

        try:
            if kind == "invite":
                result = await self._client(
                    functions.messages.ImportChatInviteRequest(value)
                )
                chats = getattr(result, "chats", None) or []
                title = getattr(chats[0], "title", None) if chats else None
                logger.info("Telegram joined a group by invite link")
                return True, f"Joined {title or 'the group'}."

            entity = await self._client.get_entity(value)
            await self._client(functions.channels.JoinChannelRequest(channel=entity))
            title = getattr(entity, "title", None) or value
            logger.info("Telegram joined a public group")
            return True, f"Joined {title}."
        except UserAlreadyParticipantError:
            return True, "This account is already a member of that group."
        except (InviteHashExpiredError, InviteHashInvalidError):
            return False, "That invite link is invalid or has expired."
        except (ChannelPrivateError, ChannelInvalidError):
            return False, "That group is private or the link does not point to one."
        except ChannelsTooMuchError:
            return False, "This account is already in too many groups."
        except Exception as exc:  # noqa: BLE001 - Telegram errors are reported, not raised
            logger.exception("Telegram group join failed")
            return False, f"Could not join that group ({type(exc).__name__})."

    async def channel_posts(
        self, target: str, limit: int = 20
    ) -> tuple[list[str], str | None]:
        """Read the newest posts of a public channel **without joining it**.

        A public channel's history is readable by any account, so asking the
        owner to join it first is busywork: a link request like «shu kanaldan
        videoning linkini tashla» must come back with real links. Each line
        carries the post's own ``t.me`` link, its media kind and a snippet, so
        the answer can quote the right one.

        Returns ``(lines, error)``. ``error`` is the sentence for the owner.
        """
        if not self.ready or self._client is None:
            return [], "Telegram is not connected, so the channel was not read."
        try:
            kind, value = _parse_group_target(target)
        except ValueError as exc:
            return [], str(exc)
        if kind != "username":
            return [], (
                "An invite link hides the channel until you join it. Send the "
                "channel's @username or a t.me/username link and I can read it "
                "without joining."
            )
        try:
            entity = await self._client.get_entity(value)
        except Exception as exc:  # noqa: BLE001 - a bad link is a normal answer
            logger.info("Telegram channel open failed (%s)", type(exc).__name__)
            return [], (
                "That channel could not be opened. A public channel needs its "
                "@username or a t.me/username link."
            )
        username = getattr(entity, "username", None) or value
        title = getattr(entity, "title", None) or username
        try:
            messages = await self._client.get_messages(
                entity, limit=max(1, min(limit, 100))
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash the turn
            logger.info("Telegram channel history failed (%s)", type(exc).__name__)
            return [], "That channel's posts could not be read."

        rows = [row for row in (_post_row(item, username) for item in messages or []) if row]
        if not rows:
            return [], f"{title} kanalida o‘qiladigan post topilmadi."
        logger.info("Telegram channel read: %s (%d posts)", title, len(rows))
        return rows, None

    async def search_channel(
        self, target: str, query: str, limit: int = 300, hits: int = 10
    ) -> tuple[list[str], str | None]:
        """Search a public channel's history without joining it.

        ``channel_posts`` only sees the newest posts, which is not enough for
        «shu kanaldan korea haqidagi videoni top»: the post may be far back.
        History is read page by page (oldest boundary included) until the post
        budget runs out, and every post whose text or links contain all the
        query words is returned.
        """
        if not self.ready or self._client is None:
            return [], "Telegram is not connected, so the channel was not read."
        terms = [word for word in re.split(r"\s+", (query or "").strip().lower()) if word]
        if not terms:
            return [], "Qidiruv so‘zi bo‘sh; hech narsa qidirilmadi."
        try:
            kind, value = _parse_group_target(target)
        except ValueError as exc:
            return [], str(exc)
        if kind != "username":
            return [], (
                "An invite link hides the channel until you join it. Send the "
                "channel's @username or a t.me/username link and I can search it "
                "without joining."
            )
        try:
            entity = await self._client.get_entity(value)
        except Exception as exc:  # noqa: BLE001 - a bad link is a normal answer
            logger.info("Telegram channel open failed (%s)", type(exc).__name__)
            return [], (
                "That channel could not be opened. A public channel needs its "
                "@username or a t.me/username link."
            )
        username = getattr(entity, "username", None) or value
        title = getattr(entity, "title", None) or username
        budget = max(20, min(limit, 2000))
        scanned = 0
        rows: list[str] = []
        try:
            async for message in self._client.iter_messages(entity, limit=budget):
                scanned += 1
                text = (getattr(message, "message", None) or "").lower()
                links = " ".join(_post_links(getattr(message, "message", None) or ""))
                haystack = f"{text} {links.lower()}"
                if not all(term in haystack for term in terms):
                    continue
                row = _post_row(message, username)
                if row:
                    rows.append(row)
                if len(rows) >= max(1, min(hits, 30)):
                    break
        except Exception as exc:  # noqa: BLE001 - report, never crash the turn
            logger.info("Telegram channel search failed (%s)", type(exc).__name__)
            if not rows:
                return [], "That channel's posts could not be searched."
        if not rows:
            return [], (
                f"{title} kanalida «{query}» bo‘yicha post topilmadi "
                f"({scanned} post ko‘rildi)."
            )
        logger.info(
            "Telegram channel search: %s (%d hits in %d posts)", title, len(rows), scanned
        )
        return rows, None

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
        """Forward messages from people who are allowed to talk.

        Trust comes only from the numeric sender id Telegram reports, never
        from what the message claims. Channels and bots are ignored: two
        automated accounts answering each other would loop forever.

        In a group the account answers only when it is addressed — an @mention
        or a reply to one of its own messages — so it can take part in a chat
        without replying to everything that is said there.
        """
        if getattr(event, "is_channel", False):
            logger.info("Ignoring a Telegram channel message")
            return
        if getattr(getattr(event, "sender", None), "bot", False):
            logger.info("Ignoring a Telegram message from a bot account")
            return
        sender_id = getattr(event, "sender_id", None)
        role = self.sender_role(sender_id)
        if role is None:
            # The numeric id is printed so the owner can be added to
            # TELEGRAM_OWNER_USER_ID instead of guessing which account wrote.
            logger.info(
                "Ignoring Telegram message from an unauthorized sender (id=%s)",
                sender_id,
            )
            return
        text = (getattr(event, "raw_text", None) or "").strip()
        if not text:
            return
        if getattr(event, "is_group", False):
            if not self._settings.telegram_groups_enabled:
                logger.info("Ignoring a group message (TELEGRAM_GROUPS_ENABLED=false)")
                return
            if not await self._addressed_in_group(event, text):
                logger.info("Ignoring a group message that does not address the account")
                return
            text = self._strip_mention(text)
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
        if person in self._settings.owner_user_ids:
            return "owner"
        if self._owner_id is not None and person == self._owner_id:
            return "owner"
        if self._self_id is not None and person == self._self_id:
            return "owner"  # the account's own "Saved Messages" chat
        if person in self._settings.chat_user_allowed_ids:
            return "guest"
        if self._settings.telegram_public_chat_enabled:
            return "guest"
        return None

    async def _addressed_in_group(self, event: Any, text: str) -> bool:
        """True when a group message is meant for this account.

        Three signals count: Telegram's own ``mentioned`` flag, an
        ``@username`` in the text, or a reply to a message this account sent.
        """
        message = getattr(event, "message", None)
        try:
            if message is not None and bool(getattr(message, "mentioned", False)):
                return True
        except Exception:  # noqa: BLE001 - a cold entity cache must not crash us
            logger.debug("Could not read the mention flag", exc_info=True)

        username = (self._self_username or "").lower()
        if username and f"@{username}" in text.lower():
            return True

        if getattr(event, "is_reply", False) and self._self_id is not None:
            try:
                replied = await event.get_reply_message()
            except Exception:  # noqa: BLE001 - deleted message or cold cache
                replied = None
            if replied is not None and getattr(replied, "sender_id", None) == self._self_id:
                return True
        return False

    def _strip_mention(self, text: str) -> str:
        """Remove this account's own @username from the text sent to the brain."""
        username = self._self_username
        if not username:
            return text.strip()
        cleaned = re.sub(
            rf"@{re.escape(username)}\b", " ", text, flags=re.IGNORECASE
        )
        return cleaned.strip() or text.strip()

    async def _handle_call_update(self, update: Any) -> None:
        if self._on_call is not None:
            try:
                await self._on_call(update)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram call handler failed")
