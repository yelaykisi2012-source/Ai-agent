"""One-to-one Telegram call handling.

Signal handling is real. Audio is not: a 1:1 Telegram call requires a WebRTC
transport (ntgcalls) that performs the Diffie-Hellman exchange, SRTP-style
media encryption and Opus framing. Until that transport is enabled and
verified end-to-end on this machine, Ksenus declines incoming calls instead of
answering with silence, and refuses to place outgoing calls instead of ringing
a phone it cannot talk to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from config import Settings
from database import record_event, session_scope
from models import CallRecord
from telegram.account import TelegramAccount
from telegram.signaling import CallEvent, classify_call_update, decline_call

logger = logging.getLogger(__name__)


class CallManager:
    """Records call signaling and applies the calling policy."""

    def __init__(self, settings: Settings, account: TelegramAccount) -> None:
        self._settings = settings
        self._account = account

    def transport_diagnostics(self) -> dict[str, Any]:
        """Report what the installed native transport can actually do."""
        try:
            import ntgcalls

            self_test = ntgcalls.NTgCalls()
            protocol = self_test.get_protocol()
            return {
                "available": True,
                "version": getattr(ntgcalls, "__version__", "unknown"),
                "library_versions": list(getattr(protocol, "library_versions", []) or []),
                "min_layer": getattr(protocol, "min_layer", None),
                "max_layer": getattr(protocol, "max_layer", None),
                "udp_p2p": getattr(protocol, "udp_p2p", None),
                "udp_reflector": getattr(protocol, "udp_reflector", None),
                "media_enabled": self._settings.telegram_calls_enabled,
                "note": (
                    "ntgcalls is installed and exposes private-call primitives. "
                    "Live audio is only enabled after verification with two "
                    "authorized accounts."
                ),
            }
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            return {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "media_enabled": False,
            }

    def can_carry_audio(self) -> bool:
        """True only when the native transport is enabled and importable."""
        if not self._settings.telegram_calls_enabled:
            return False
        try:
            import ntgcalls  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    async def handle_update(self, update: Any) -> None:
        """Process an ``UpdatePhoneCall`` from Telethon."""
        event = classify_call_update(update)
        if event is None:
            return
        if event.state == "discarded":
            self._close_best_effort(event)
            return

        if event.direction != "incoming":
            return

        allowed = (
            self._settings.telegram_allow_incoming_calls
            and self.can_carry_audio()
            and not self._paused()
        )
        if allowed:
            reason = (
                "Auto-answer is enabled, but the live audio pipeline has not "
                "been verified on this machine; the call was declined."
            )
            state = "declined"
        else:
            reason = (
                "Incoming call declined: live Telegram audio transport is "
                "disabled. Enable TELEGRAM_CALLS_ENABLED only after verifying "
                "the media stack."
            )
            state = "declined"

        declined = await decline_call(self._account.client, event)
        self._record(event, state if declined else "decline_failed", reason)
        record_event(
            source="telegram_call",
            message=f"Incoming Telegram call {event.call_id} from {event.peer_id}",
            level="WARNING" if not declined else "INFO",
            details=reason,
        )
        await self._account.send_message(
            "I declined an incoming Telegram call because live call audio is "
            "not enabled yet. Send me a message instead."
        )

    async def initiate(self, reason: str) -> tuple[bool, str]:
        """Refuse to place a call until the media stack is verified."""
        if not self.can_carry_audio():
            return (
                False,
                "I cannot place a Telegram call yet: the live audio transport is "
                "disabled or unavailable. Nothing was dialled.",
            )
        return (
            False,
            "Live Telegram calling is installed but not verified with two "
            "accounts, so I will not ring your phone and leave you in silence.",
        )

    def history(self, limit: int = 50) -> tuple[list[CallRecord], int]:
        with session_scope() as session:
            stmt = select(CallRecord).order_by(CallRecord.id.desc()).limit(limit)
            rows = list(session.scalars(stmt))
            total = session.scalar(select(func.count()).select_from(CallRecord)) or 0
        return rows, int(total)

    # -- internals -------------------------------------------------------- #
    @staticmethod
    def _paused() -> bool:
        from memory import is_paused

        return is_paused()

    @staticmethod
    def _record(event: CallEvent, state: str, reason: str) -> None:
        with session_scope() as session:
            session.add(
                CallRecord(
                    direction=event.direction,
                    peer_id=event.peer_id,
                    state=state,
                    reason=reason,
                    started_at=datetime.now(timezone.utc),
                )
            )

    @staticmethod
    def _close_best_effort(event: CallEvent) -> None:
        with session_scope() as session:
            row = session.scalars(
                select(CallRecord)
                .where(CallRecord.peer_id == event.peer_id)
                .order_by(CallRecord.id.desc())
            ).first()
            if row is not None and row.ended_at is None:
                row.ended_at = datetime.now(timezone.utc)
