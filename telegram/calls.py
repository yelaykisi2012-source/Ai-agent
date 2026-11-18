"""One-to-one Telegram call management.

The manager is the single owner of the call lifecycle: it decides whether a
call may start, keeps exactly one live session, routes MTProto signaling and
audio frames and records every call. The owner is dialled on request; anyone
else is dialled only after that person explicitly agreed, and the agreement
expires.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from config import Settings
from database import record_event, session_scope
from models import CallRecord
from telegram.media import MediaError, NtgTransport, protocol_info
from telegram.signaling import build_decline_request
from telegram.voice_call import VoiceCallSession

logger = logging.getLogger(__name__)


class CallManager:
    """Owns at most one live Telegram call and its audit trail."""

    def __init__(
        self,
        settings: Settings,
        account: Any,
        *,
        transport: Any | None = None,
    ) -> None:
        self._settings = settings
        self._account = account
        self._transport = transport if transport is not None else NtgTransport(settings)
        self._speech: Any | None = None
        self._on_message: Any | None = None
        self._on_schedule_call: Any | None = None
        self._session: VoiceCallSession | None = None
        self._pending_incoming: Any | None = None
        self._pending_caller: int | None = None
        self._lock = asyncio.Lock()
        #: "Yes, call me" from someone who is not the owner: user id -> expiry
        #: timestamp. Deliberately kept in this process only, so a restart asks
        #: again instead of reusing an agreement nobody repeated.
        self._call_consent: dict[int, float] = {}
        #: Diagnostics: how many signaling packets reached Telegram and failed.
        self._signaling_relayed = 0
        self._signaling_failures = 0

        for name, handler in (
            ("on_frames", self._route_frames),
            ("on_signaling_data", self._route_signaling),
            ("on_connection_change", self._route_connection),
        ):
            register = getattr(self._transport, name, None)
            if register is not None:
                register(handler)

    # -- wiring ----------------------------------------------------------- #
    def bind_pipeline(
        self, speech: Any, on_message: Any, on_schedule_call: Any = None
    ) -> None:
        """Attach the speech engine and the shared chat handler."""
        self._speech = speech
        self._on_message = on_message
        #: Optional: stores "call me in N minutes" from inside a live call.
        self._on_schedule_call = on_schedule_call

    @property
    def owner_id(self) -> int | None:
        """The only Telegram user Ksenus may call or accept calls from."""
        configured = self._settings.telegram_owner_user_id
        if configured is not None:
            return configured
        try:
            import memory

            stored = memory.get_state("telegram_owner_user_id")
            if stored:
                return int(stored)
        except Exception:  # noqa: BLE001 - no database yet is normal
            logger.debug("Telegram call: stored owner id unavailable", exc_info=True)
        candidate = getattr(self._account, "owner_id", None)
        self_id = getattr(self._account, "self_id", None)
        if candidate is not None and candidate != self_id:
            return int(candidate)
        return None

    @property
    def active(self) -> bool:
        session = self._session
        return bool(session is not None and session.active)

    def can_carry_audio(self) -> bool:
        """True only when live audio is enabled and the native transport loads."""
        if not self._settings.telegram_calls_enabled:
            return False
        return bool(getattr(self._transport, "available", False))

    # -- public actions --------------------------------------------------- #
    async def place(
        self, reason: str = "owner request", *, peer_id: int | None = None
    ) -> tuple[bool, str]:
        """Call the owner, or another person who agreed to it.

        ``peer_id`` defaults to the owner. Any other target must be an allowed
        chat user and must have given explicit, unexpired consent; otherwise
        nothing is dialled and the reason is returned.
        """
        if not self._settings.telegram_calls_enabled:
            return (
                False,
                "I cannot place a Telegram call: live call audio is disabled "
                "(TELEGRAM_CALLS_ENABLED=false). Nothing was dialled.",
            )
        if not getattr(self._transport, "available", False):
            return (
                False,
                "I cannot place a Telegram call: the native audio transport is "
                "unavailable. Nothing was dialled.",
            )
        if not getattr(self._account, "ready", False):
            return (
                False,
                "I cannot place a Telegram call: the Telegram account is not "
                "connected. Nothing was dialled.",
            )
        if self._speech is None or not getattr(self._speech, "configured", False):
            return (
                False,
                "I cannot place a Telegram call: speech recognition/synthesis is "
                "not configured. Nothing was dialled.",
            )
        owner = self.owner_id
        if owner is None:
            return (
                False,
                "I cannot place a Telegram call: the owner is not configured. Set "
                "TELEGRAM_OWNER_USER_ID to your Telegram user id. Nothing was dialled.",
            )
        self_id = getattr(self._account, "self_id", None)
        if self_id is not None and owner == self_id:
            return (
                False,
                "I cannot place a Telegram call to my own account. Set "
                "TELEGRAM_OWNER_USER_ID to your personal account. Nothing was dialled.",
            )

        target = int(peer_id) if peer_id is not None else owner
        if self_id is not None and target == self_id:
            return (
                False,
                "I cannot place a Telegram call to my own account. Nothing was dialled.",
            )
        if target != owner:
            refusal = self._check_guest_call(target)
            if refusal is not None:
                logger.warning("Telegram call: refused (%s)", refusal[:120])
                return False, refusal

        async with self._lock:
            if self._session is not None and self._session.active:
                return (
                    False,
                    "A Telegram call is already in progress; I will not start a second one.",
                )
            session = VoiceCallSession(
                self._settings,
                self._transport,
                self._account.client,
                peer_id=target,
                speech=self._speech,
                on_message=self._peer_handler(target),
                on_finished=self._on_session_finished,
                on_schedule_call=self._on_schedule_call,
                direction="outgoing",
            )
            logger.info(
                "Telegram call: placing a call because of %s (owner=%s)",
                reason[:120],
                target == owner,
            )
            started, detail = await session.place()
            self._session = session if started else None
            if started:
                self._open_record(session)
                if target != owner:
                    # One agreement, one call: the next call needs a fresh yes.
                    self.revoke_call_consent(target)
            else:
                self._record(
                    direction="outgoing",
                    peer_id=target,
                    state="failed",
                    reason=detail,
                )
                await self._safe_send(f"Telegram call failed: {detail}")
            return started, detail

    async def answer(self) -> tuple[bool, str]:
        """Answer the incoming call that is waiting for approval."""
        if self._pending_incoming is None:
            return False, "There is no incoming Telegram call waiting to be answered."
        if not self.can_carry_audio():
            return (
                False,
                "I cannot answer the call: live audio transport is disabled.",
            )
        async with self._lock:
            if self._session is not None and self._session.active:
                # A guest call answered meanwhile must not be replaced by this
                # one: two live sessions would fight over the same microphone.
                return (
                    False,
                    "A Telegram call is already in progress; I did not answer another one.",
                )
            pending_caller = self._pending_caller
            owner = self.owner_id
            if owner is None or pending_caller not in (None, owner):
                self._pending_incoming = None
                self._pending_caller = None
                logger.warning(
                    "Telegram call: a waiting call was not from the owner; dropped it"
                )
                return False, "That waiting call is not from the owner; it was not answered."
            requested = self._pending_incoming
            self._pending_incoming = None
            self._pending_caller = None
            session = VoiceCallSession(
                self._settings,
                self._transport,
                self._account.client,
                peer_id=pending_caller or 0,
                speech=self._speech,
                on_message=self._peer_handler(pending_caller or 0),
                on_finished=self._on_session_finished,
                on_schedule_call=self._on_schedule_call,
                direction="incoming",
            )
            answered, detail = await session.accept(requested)
            self._session = session if answered else None
            if answered:
                self._open_record(session)
            return answered, detail

    async def hangup(self, reason: str = "owner_hangup") -> tuple[bool, str]:
        session = self._session
        if session is None or not session.active:
            return False, "There is no active Telegram call to end."
        logger.info("Telegram call: hanging up (%s)", reason)
        return await session.hangup(reason)

    async def initiate(
        self, reason: str, *, peer_id: int | None = None
    ) -> tuple[bool, str]:
        """Backwards-compatible alias for :meth:`place`."""
        return await self.place(reason, peer_id=peer_id)

    # -- calling someone who is not the owner ----------------------------- #
    def grant_call_consent(self, user_id: int) -> None:
        """Record that this person just said yes to being called."""
        self._call_consent[int(user_id)] = (
            time.monotonic() + self._settings.telegram_call_consent_seconds
        )
        logger.info("Telegram call: user %s agreed to be called", user_id)

    def revoke_call_consent(self, user_id: int) -> None:
        self._call_consent.pop(int(user_id), None)

    def has_call_consent(self, user_id: int) -> bool:
        """True only for an unexpired, explicit yes from this person."""
        expires_at = self._call_consent.get(int(user_id))
        if expires_at is None:
            return False
        if expires_at <= time.monotonic():
            self._call_consent.pop(int(user_id), None)
            return False
        return True

    def _check_guest_call(self, target: int) -> str | None:
        """Return the refusal text for a call to a non-owner, or None to allow it."""
        if (
            not self._settings.telegram_public_chat_enabled
            and target not in self._settings.chat_user_allowed_ids
        ):
            return (
                "I will not call that person: they are not the owner and they are "
                "not on TELEGRAM_ALLOWED_USER_IDS. Nothing was dialled."
            )
        if not self.has_call_consent(target):
            return (
                "I will not call that person yet: they have not agreed to a call. "
                "Ask them and wait for an explicit yes. Nothing was dialled."
            )
        return None

    def _peer_handler(self, peer_id: int) -> Any:
        """Bind the shared chat handler to one peer.

        ``VoiceCallSession`` keeps calling ``on_message(text)`` exactly as
        before; the peer id is added here. That keeps the call and audio code
        untouched while the runtime can hold one conversation per person.
        """
        handler = self._on_message

        async def call_handler(text: str) -> Any:
            if handler is None:
                return None
            return await handler(text, peer_id)

        return call_handler

    # -- signaling -------------------------------------------------------- #
    async def handle_update(self, update: Any) -> None:
        """Route one raw Telegram update into the call layer."""
        name = type(update).__name__

        if name == "UpdatePhoneCallSignalingData":
            session = self._session
            data = getattr(update, "data", None)
            if session is None or not data:
                return
            # Signaling belongs to exactly one call: the update carries its
            # phone_call_id, which is neither the peer id nor the transport id.
            update_call_id = getattr(update, "phone_call_id", None)
            if (
                update_call_id is not None
                and session.call_id is not None
                and int(update_call_id) != session.call_id
            ):
                logger.debug(
                    "Telegram call: signaling for call_id=%s ignored (active call_id=%s)",
                    update_call_id,
                    session.call_id,
                )
                return
            await session.handle_signaling(bytes(data))
            return

        if name != "UpdatePhoneCall":
            return
        call = getattr(update, "phone_call", None)
        if call is None:
            return

        if type(call).__name__ == "PhoneCallRequested":
            await self._on_incoming(call)
            return

        session = self._session
        if session is not None:
            # A late update from a previous call (or the peer's next call) must
            # not touch the live session: call ids are per call, not per peer.
            if not session.matches(call):
                logger.debug(
                    "Telegram call: %s for call_id=%s ignored (active call_id=%s)",
                    type(call).__name__,
                    getattr(call, "id", None),
                    session.call_id,
                )
                return
            await session.handle_phone_call(call)
            return

        # No live session: this may be the owner's call ending before it was
        # approved. Clear it, otherwise every later call is refused as busy.
        if self._clear_pending_if_matches(call):
            return

        record_event(
            source="telegram_call",
            message=f"Unmatched call update ({type(call).__name__})",
            details="No live session owns this update; nothing was answered.",
        )

    async def _on_incoming(self, call: Any) -> None:
        caller = self._remote_user_id(call)
        owner = self.owner_id

        if self._session is not None and self._session.active:
            await self._decline(
                call,
                state="declined",
                detail="Another Telegram call is already in progress; the new call was declined.",
                busy=True,
            )
            return

        if not self.can_carry_audio():
            await self._decline(
                call,
                state="declined",
                detail=(
                    "Incoming call declined: live Telegram audio transport is "
                    "disabled. Enable TELEGRAM_CALLS_ENABLED only after verifying "
                    "the media stack."
                ),
            )
            return

        self_id = getattr(self._account, "self_id", None)
        is_owner = owner is not None and caller == owner
        if not is_owner:
            # Anyone may call when public calls are enabled. The caller is a
            # guest: the runtime gives them guest rules (no tools, no owner
            # data, redacted replies), so answering is safe.
            if not self._settings.telegram_public_calls_enabled:
                await self._decline(
                    call,
                    state="declined",
                    detail=(
                        "Incoming call declined: the caller is not the configured "
                        "owner and public calls are disabled."
                    ),
                )
                return
            if caller is None or (self_id is not None and caller == self_id):
                await self._decline(
                    call,
                    state="declined",
                    detail="Incoming call declined: the caller could not be identified.",
                )
                return
            logger.info(
                "Telegram call: incoming call from a guest (public calls enabled)"
            )
            record_event(
                source="telegram_call",
                message="Incoming call from a guest",
                details="Guest rules apply: no tools and no owner data.",
            )

        if self._pending_incoming is not None:
            # The owner's call is waiting for approval and has priority: a guest
            # must not take the line while the owner is about to be connected.
            await self._decline(
                call,
                state="declined",
                detail=(
                    "Another incoming call is already waiting for approval; this "
                    "call was declined."
                ),
                busy=True,
            )
            return

        if is_owner and not self._settings.telegram_call_auto_answer:
            self._pending_incoming = call
            self._pending_caller = caller
            logger.info("Telegram call: owner is calling; answering is waiting for approval")
            record_event(
                source="telegram_call",
                message="Incoming call from the owner is waiting for approval",
                details="Live audio is enabled; answer it explicitly.",
            )
            await self._safe_send(
                "I see your Telegram call. Answering is waiting for your approval."
            )
            return

        async with self._lock:
            # Re-check under the lock: two updates can arrive close together,
            # and accepting both would leak one call's audio into the other.
            if self._session is not None and self._session.active:
                await self._decline(
                    call,
                    state="declined",
                    detail=(
                        "Another Telegram call is already in progress; the new "
                        "call was declined."
                    ),
                    busy=True,
                )
                return
            session = VoiceCallSession(
                self._settings,
                self._transport,
                self._account.client,
                peer_id=caller,
                speech=self._speech,
                on_message=self._peer_handler(caller),
                on_finished=self._on_session_finished,
                on_schedule_call=self._on_schedule_call,
                direction="incoming",
            )
            answered, detail = await session.accept(call)
            self._session = session if answered else None
            if answered:
                self._open_record(session)
            else:
                record_event(
                    source="telegram_call",
                    message="Incoming call could not be answered",
                    level="WARNING",
                    details=detail,
                )

    def _clear_pending_if_matches(self, call: Any) -> bool:
        """Forget the owner's waiting call once that same call has ended.

        Without this a cancelled call stays "pending" forever and every later
        caller, guest or owner, is refused as busy.
        """
        pending = self._pending_incoming
        if pending is None:
            return False
        if getattr(call, "id", None) != getattr(pending, "id", None):
            return False
        self._pending_incoming = None
        self._pending_caller = None
        logger.info(
            "Telegram call: the waiting incoming call ended before it was approved"
        )
        record_event(
            source="telegram_call",
            message="Waiting incoming call ended before approval",
            details=f"{type(call).__name__}; the line is free again.",
        )
        return True

    async def _decline(
        self,
        call: Any,
        *,
        state: str,
        detail: str,
        busy: bool = False,
    ) -> None:
        declined = False
        try:
            request = build_decline_request(
                int(call.id),
                int(call.access_hash),
                busy=busy,
            )
            await self._account.client(request)
            declined = True
        except Exception:  # noqa: BLE001 - remote failures must not crash the client
            logger.exception("Telegram call: could not decline the incoming call")

        self._record(
            direction="incoming",
            peer_id=self._remote_user_id(call) or 0,
            state=state if declined else "decline_failed",
            reason=detail,
        )
        record_event(
            source="telegram_call",
            message=(
                f"Incoming Telegram call declined ({'busy' if busy else 'not allowed'})"
            ),
            level="WARNING" if not declined else "INFO",
            details=detail,
        )
        await self._safe_send(
            "I declined an incoming Telegram call because live call audio is "
            "not available for it right now. Send me a message instead."
        )

    def _remote_user_id(self, call: Any) -> int | None:
        admin = getattr(call, "admin_id", None)
        participant = getattr(call, "participant_id", None)
        self_id = getattr(self._account, "self_id", None)
        if self_id is not None and admin == self_id:
            return int(participant) if participant is not None else None
        if admin is not None:
            return int(admin)
        return int(participant) if participant is not None else None

    # -- transport callbacks ---------------------------------------------- #
    def _route_frames(self, chat_id: int, mode: str, device: str, data: bytes) -> None:
        session = self._session
        if session is not None and session.peer_id == chat_id:
            session.push_frames(chat_id, mode, device, data)

    def _route_signaling(self, chat_id: int, data: bytes) -> None:
        """Relay ntgcalls signaling to the peer.

        ntgcalls fires this callback on its own native thread, so the MTProto
        send must be handed to the session's event loop thread-safely. A plain
        ``loop.create_task`` from that thread is undefined behaviour and can
        lose the peer's ICE packet, which then times the call out.
        """
        session = self._session
        if session is None or session.peer_id != chat_id or not data:
            return
        loop = getattr(session, "_loop", None)
        if loop is None:
            logger.debug("Telegram call: signaling dropped; the session loop is not ready")
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._relay_signaling(session, bytes(data)), loop
            )
        except RuntimeError:  # noqa: BLE001 - a closed loop is the normal shutdown case
            logger.debug("Telegram call: signaling dropped; the loop is closed")
            return
        future.add_done_callback(self._log_relay_result)

    @staticmethod
    def _log_relay_result(future: Any) -> None:
        """Consume the relayed future so a failure is logged, not lost."""
        try:
            future.exception()
        except Exception:  # noqa: BLE001 - cancelled futures are expected at shutdown
            logger.debug("Telegram call: signaling relay did not finish", exc_info=True)

    async def _relay_signaling(self, session: VoiceCallSession, data: bytes) -> None:
        if session.call_id is None or session.access_hash is None:
            return
        from telethon.tl import types

        await self._send_signaling(
            types.InputPhoneCall(id=session.call_id, access_hash=session.access_hash),
            data,
        )

    async def _send_signaling(self, peer: Any, data: bytes) -> None:
        from telethon.tl import functions

        try:
            await self._account.client(
                functions.phone.SendSignalingDataRequest(peer=peer, data=data)
            )
            self._signaling_relayed += 1
            if self._signaling_relayed == 1:
                logger.info(
                    "Telegram call: first signaling packet relayed to Telegram (%d bytes)",
                    len(data),
                )
        except Exception as exc:  # noqa: BLE001 - ICE retries are expected to fail sometimes
            self._signaling_failures += 1
            # A rejected relay silently breaks ICE, so the first failures are
            # visible; later ones stay at debug to avoid a log flood.
            log = logger.warning if self._signaling_failures <= 3 else logger.debug
            log(
                "Telegram call: sending signaling data failed (%s, %d bytes)",
                type(exc).__name__,
                len(data),
                exc_info=self._signaling_failures <= 3,
            )

    def _route_connection(self, chat_id: int, state: str) -> None:
        session = self._session
        if session is not None and session.peer_id == chat_id:
            session.connection_changed(chat_id, state)
            # Also register the chat_id with the transport for frame sending
            if hasattr(self._transport, 'register_connection_chat_id'):
                self._transport.register_connection_chat_id(session.peer_id, chat_id)

    # -- records ---------------------------------------------------------- #
    def _open_record(self, session: VoiceCallSession) -> None:
        try:
            with session_scope() as db:
                row = CallRecord(
                    direction=session.direction,
                    peer_id=session.peer_id,
                    state="active",
                    reason=None,
                    started_at=session.started_at,
                )
                db.add(row)
                db.flush()
                session.record_id = int(row.id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - auditing must not break the call
            logger.exception("Telegram call: could not open the call record")
        record_event(
            source="telegram_call",
            message=f"Telegram call started ({session.direction})",
            details=f"call_id={session.call_id}",
        )

    async def _on_session_finished(
        self, session: VoiceCallSession, reason: str, detail: str
    ) -> None:
        record_id = getattr(session, "record_id", None)
        state = "ended" if reason not in {"failed"} else "failed"
        reason_text = f"{reason}: {detail}" if detail else reason
        try:
            with session_scope() as db:
                row = db.get(CallRecord, record_id) if record_id else None
                if row is None:
                    db.add(
                        CallRecord(
                            direction=session.direction,
                            peer_id=session.peer_id,
                            state=state,
                            reason=reason_text,
                            started_at=session.started_at,
                            ended_at=datetime.now(timezone.utc),
                        )
                    )
                else:
                    row.state = state
                    row.reason = reason_text
                    row.ended_at = datetime.now(timezone.utc)
        except Exception:  # noqa: BLE001
            logger.exception("Telegram call: could not close the call record")

        if self._session is session:
            self._session = None
        record_event(
            source="telegram_call",
            message=f"Telegram call finished ({reason})",
            level="INFO" if state == "ended" else "WARNING",
            details=f"detail={detail or '-'}, utterances={session._utterances}, replies={session._replies}",
        )
        await self._safe_send(
            f"Telegram call ended ({session.end_reason_text(reason)})."
        )

    def _record(self, *, direction: str, peer_id: int, state: str, reason: str) -> None:
        try:
            with session_scope() as db:
                db.add(
                    CallRecord(
                        direction=direction,
                        peer_id=peer_id,
                        state=state,
                        reason=reason,
                        started_at=datetime.now(timezone.utc),
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram call: could not write the call record")

    async def _safe_send(self, text: str) -> None:
        sender = getattr(self._account, "send_message", None)
        if sender is None:
            return
        try:
            await sender(text)
        except Exception:  # noqa: BLE001 - notifications are best effort
            logger.debug("Telegram call: notification failed", exc_info=True)

    # -- diagnostics ------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        session = self._session
        return {
            "calls_enabled": self._settings.telegram_calls_enabled,
            "auto_answer": self._settings.telegram_call_auto_answer,
            "public_calls_enabled": self._settings.telegram_public_calls_enabled,
            "transport_available": bool(getattr(self._transport, "available", False)),
            "owner_configured": self.owner_id is not None,
            "active": bool(session is not None and session.active),
            "pending_incoming": self._pending_incoming is not None,
            "session": session.status() if session is not None else None,
        }

    def transport_diagnostics(self) -> dict[str, Any]:
        """Report what the installed native transport can actually do."""
        info = protocol_info()
        devices: dict[str, list[str]] = {"microphones": [], "speakers": []}
        lister = getattr(self._transport, "devices", None)
        if lister is not None:
            try:
                devices = lister()
            except Exception:  # noqa: BLE001
                logger.debug("Telegram call: device listing failed", exc_info=True)

        available = bool(getattr(self._transport, "available", False))
        version = getattr(self._transport, "version", "unknown")
        stats: dict[str, Any] = {}
        stats_getter = getattr(self._transport, "stats", None)
        if stats_getter is not None:
            try:
                stats = dict(stats_getter())
            except Exception:  # noqa: BLE001 - diagnostics must not raise
                logger.debug("Telegram call: transport stats failed", exc_info=True)
        return {
            "available": available,
            "version": version,
            "transport_stats": stats,
            "signaling_relayed": self._signaling_relayed,
            "signaling_failures": self._signaling_failures,
            "library_versions": list(info.library_versions) if info else [],
            "min_layer": info.min_layer if info else None,
            "max_layer": info.max_layer if info else None,
            "udp_p2p": info.udp_p2p if info else None,
            "udp_reflector": info.udp_reflector if info else None,
            "devices": devices,
            "media_enabled": self._settings.telegram_calls_enabled,
            "active_call": self.active,
            "note": (
                "ntgcalls provides the encrypted media transport. Live audio is "
                "only used for calls to the configured owner."
            ),
        }

    def history(self, limit: int = 50) -> tuple[list[CallRecord], int]:
        with session_scope() as session:
            stmt = select(CallRecord).order_by(CallRecord.id.desc()).limit(limit)
            rows = list(session.scalars(stmt))
            total = session.scalar(select(func.count()).select_from(CallRecord)) or 0
        return rows, int(total)

    @staticmethod
    def _paused() -> bool:
        from memory import is_paused

        return is_paused()


__all__ = ["CallManager", "MediaError"]
