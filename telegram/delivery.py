"""Deliver prepared material to the owner's own Telegram chat.

Only ``TELEGRAM_OWNER_USER_ID`` is ever used as the destination: there is no
parameter for a different chat anywhere in this module or its tool schema, and
no bot token is involved — the same Telethon user account that answers calls
sends the message.

A result is reported as successful only when Telethon returned a message
object. Content that looks like an API key, token or password is refused
instead of being sent.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)

#: One Telegram text message holds 4096 characters; leave room for the header.
_MAX_MESSAGE_CHARS = 4000
#: Identical payloads inside this window are not sent twice.
_DUPLICATE_WINDOW_SECONDS = 600.0
#: One retry covers a dropped connection without spamming the chat.
_SEND_ATTEMPTS = 2
_RETRY_PAUSE_SECONDS = 0.8
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_ALLOWED_EXTENSIONS = {".txt", ".md", ".py", ".csv", ".json", ".log", ".html", ".xlsx"}
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"),
)


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one delivery attempt."""

    ok: bool
    detail: str
    filename: str | None = None
    size: int = 0


def _looks_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _safe_filename(name: str | None, extension: str) -> str:
    stem = _SAFE_NAME.sub("-", (name or "").strip()).strip("-._") or "ksenus"
    stem = stem[:60]
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    return f"{stem}-{stamp}{extension}"


def _rows_from_text(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]


def _table_bytes(rows: list[list[Any]], table_format: str) -> bytes:
    """Build a CSV or XLSX payload from rows."""
    if table_format == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Ksenus"
        for row in rows:
            sheet.append([_cell(value) for value in row])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    return buffer.getvalue().encode("utf-8")


def _cell(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = "" if value is None else str(value)
    # A leading '=' would be executed as a formula in a spreadsheet.
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


class TelegramDelivery:
    """Builds and sends text, code, tables and files to the owner only."""

    def __init__(
        self,
        settings: Settings,
        account: Any,
        *,
        recorder: Callable[..., None] | None = None,
    ) -> None:
        self._settings = settings
        self._account = account
        self._sent: dict[str, float] = {}
        #: Optional execution-history writer (``database.record_event``).
        self._recorder = recorder

    # -- public ----------------------------------------------------------- #
    @property
    def owner_id(self) -> int | None:
        return self._settings.telegram_owner_user_id

    @property
    def configured(self) -> bool:
        return self.owner_id is not None and bool(getattr(self._account, "ready", False))

    async def send(
        self,
        *,
        kind: str,
        content: str = "",
        rows: list[list[Any]] | None = None,
        filename: str | None = None,
        table_format: str = "xlsx",
        caption: str | None = None,
    ) -> DeliveryResult:
        if self.owner_id is None:
            return DeliveryResult(
                False,
                "Not sent: TELEGRAM_OWNER_USER_ID is not configured, so there is no "
                "verified private chat to send to.",
            )
        if not getattr(self._account, "ready", False):
            return DeliveryResult(
                False, "Not sent: the Telegram account is not connected right now."
            )

        kind = (kind or "text").strip().lower()
        raw = content or ""
        text = raw.strip()

        if kind == "text":
            if not text:
                return DeliveryResult(False, "Not sent: there was no text to send.")
            if _looks_secret(text):
                logger.warning("Telegram delivery: refused content that looks secret")
                return DeliveryResult(
                    False,
                    "Not sent: the content looks like an API key, token or password, "
                    "so I did not send it.",
                )
            if len(text) <= _MAX_MESSAGE_CHARS:
                key = self._key("text", "", text)
                duplicate = self._duplicate(key)
                if duplicate is not None:
                    return duplicate
                sent = await self._attempt(
                    lambda: self._account.send_message(text, user_id=self.owner_id)
                )
                if not sent:
                    logger.error("Telegram delivery: the message was rejected")
                    return DeliveryResult(False, "Not sent: Telegram rejected the message.")
                self._remember(key)
                logger.info("Telegram delivery: sent a %d-character message", len(text))
                self._record("Sent a message", f"{len(text)} characters")
                return DeliveryResult(True, "Sent the text to your private Telegram chat.")
            # Too long for one message: send it as a file instead of truncating.
            payload = text.encode("utf-8")
            name = _safe_filename(filename or "xabar", ".txt")
            return await self._send_document(name, payload, caption, "text")

        if kind == "code":
            if not text:
                return DeliveryResult(False, "Not sent: there was no code to send.")
            payload = raw.encode("utf-8")  # keep the code byte-for-byte
            extension = ".txt" if (filename or "").lower().endswith(".txt") else ".py"
            name = _safe_filename(filename or "kod", extension)
            return await self._send_document(name, payload, caption, "code")

        if kind == "table":
            table_rows = rows or _rows_from_text(text)
            if not table_rows:
                return DeliveryResult(False, "Not sent: the table had no rows.")
            table_format = "csv" if table_format == "csv" else "xlsx"
            payload = _table_bytes(table_rows, table_format)
            extension = ".csv" if table_format == "csv" else ".xlsx"
            name = _safe_filename(filename or "jadval", extension)
            return await self._send_document(name, payload, caption, "table")

        if kind == "file":
            if not text:
                return DeliveryResult(False, "Not sent: the file had no content.")
            requested = (filename or "fayl.txt").strip()
            extension = "." + requested.rsplit(".", 1)[-1].lower() if "." in requested else ".txt"
            if extension not in _ALLOWED_EXTENSIONS:
                return DeliveryResult(
                    False,
                    f"Not sent: {extension} files are not supported. Allowed: "
                    + ", ".join(sorted(_ALLOWED_EXTENSIONS)),
                )
            payload = raw.encode("utf-8")
            name = _safe_filename(requested.rsplit(".", 1)[0], extension)
            return await self._send_document(name, payload, caption, "file")

        return DeliveryResult(False, f"Not sent: unknown delivery kind {kind!r}.")

    # -- internals -------------------------------------------------------- #
    async def _send_document(
        self, name: str, payload: bytes, caption: str | None, kind: str
    ) -> DeliveryResult:
        if not payload:
            return DeliveryResult(False, "Not sent: the file would be empty.", name)
        limit = self._settings.telegram_delivery_max_mb * 1024 * 1024
        if len(payload) > limit:
            return DeliveryResult(
                False,
                f"Not sent: the file is {len(payload) / 1024 / 1024:.1f} MB and the limit is "
                f"{self._settings.telegram_delivery_max_mb} MB.",
                name,
            )
        text_preview = payload.decode("utf-8", errors="ignore")
        if _looks_secret(text_preview):
            return DeliveryResult(
                False,
                "Not sent: the content looks like an API key, token or password, "
                "so I did not send it.",
                name,
            )

        # The generated filename contains a timestamp, so key on content only:
        # the same material must not be uploaded twice in one call.
        key = self._key(kind, "", text_preview)
        duplicate = self._duplicate(key)
        if duplicate is not None:
            return duplicate

        sent = await self._attempt(
            lambda: self._account.send_document(
                name, payload, caption=caption, user_id=self.owner_id
            )
        )
        if not sent:
            logger.error("Telegram delivery: upload of %s was not confirmed", name)
            return DeliveryResult(
                False, "Not sent: Telegram did not confirm the upload.", name, len(payload)
            )
        self._remember(key)
        logger.info("Telegram delivery: sent %s (%d bytes)", name, len(payload))
        self._record(f"Sent {name}", f"{len(payload)} bytes")
        size_kb = len(payload) / 1024
        return DeliveryResult(
            True,
            f"Sent {name} ({size_kb:.0f} KB) to your private Telegram chat.",
            name,
            len(payload),
        )

    async def _attempt(self, send: Callable[[], Any]) -> bool:
        """Send, retrying once: a dropped connection should not lose the file."""
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            if await send():
                return True
            if attempt < _SEND_ATTEMPTS:
                logger.warning("Telegram delivery: send failed; retrying once")
                await asyncio.sleep(_RETRY_PAUSE_SECONDS)
        return False

    def _record(self, message: str, details: str) -> None:
        """Write to the execution history; never let that break a delivery."""
        if self._recorder is None:
            return
        try:
            self._recorder(message=message, details=details)
        except Exception:  # noqa: BLE001 - audit logging must stay optional
            logger.warning("Telegram delivery: could not record the event")

    def _key(self, kind: str, name: str, payload: str) -> str:
        digest = hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()
        return f"{kind}:{name}:{digest}"

    def _duplicate(self, key: str) -> DeliveryResult | None:
        now = time.monotonic()
        for stale in [k for k, at in self._sent.items() if now - at > _DUPLICATE_WINDOW_SECONDS]:
            self._sent.pop(stale, None)
        if key in self._sent:
            return DeliveryResult(
                True,
                "This exact item was already sent a moment ago; I did not send a duplicate.",
            )
        return None

    def _remember(self, key: str) -> None:
        self._sent[key] = time.monotonic()
