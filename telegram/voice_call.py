"""One live Telegram voice call.

The session owns the MTProto signaling for a single 1:1 call, drives the
ntgcalls transport and runs the audio conversation:

    peer audio -> inbound frames -> VAD -> WAV -> STT -> brain -> TTS
               -> 24 kHz PCM -> 48 kHz frames -> ntgcalls -> peer

Only one session can be active at a time (the manager enforces that). Nothing
here logs phone numbers, login codes, 2FA passwords or session data; only the
numeric user id, the call id and counters are written.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import hashlib
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from telethon.tl import functions, types

from config import Settings
from telegram.audio import (
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    SILENCE_FRAME,
    TTS_SAMPLE_RATE,
    is_speech,
    pcm16_to_wav,
    pcm_duration_ms,
    trim_silence_edges,
    split_frames,
    upsample_24k_to_48k,
)
from telegram.media import MediaError, build_phone_call_protocol
from telegram.farewell import Farewell, callback_delay_minutes, farewell_match
from voice.uzbek import (
    language_report,
    prepare_for_tts,
    strip_foreign_openers,
    unread_digits,
)

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str], Awaitable[str | None]]
FinishedHandler = Callable[["VoiceCallSession", str, str], None]

#: Errors that mean the native call no longer exists (for example
#: ``ntgcalls.ConnectionNotFound`` after the transport was closed). A frame
#: cannot be sent any more, but it is a lost connection, not an audio bug.
_CONNECTION_LOST_ERRORS = {
    "ConnectionNotFound",
    "ConnectionError",
    "ClosedError",
}

#: Bounds for signaling packets held until ``connect_p2p`` has created the
#: native signaling channel. Telegram sends the peer's initial setup and ICE
#: candidates in exactly that window; they must never be dropped.
_MAX_SIGNALING_BACKLOG_PACKETS = 512
_MAX_SIGNALING_BACKLOG_BYTES = 1 << 20

#: Utterances waiting behind the reply in flight. Bounded so stale speech
#: cannot pile up while STT/LLM/TTS is slow; the oldest one is dropped.
_MAX_PENDING_UTTERANCES = 3
#: A repeated transcript inside this window is treated as the same utterance.
_TRANSCRIPT_REPEAT_WINDOW_SECONDS = 8.0
#: Bounded cache of audio -> transcript so identical audio is not re-sent.
_MAX_STT_CACHE = 8
#: A stalled playback queue must not block the reply worker forever.
_OUTBOUND_PUT_TIMEOUT_SECONDS = 1.0
#: Natural break between two synthesized sentences (they are trimmed first).
_SENTENCE_GAP_MS = 60
#: A queued utterance that waited longer than this while a newer one is also
#: waiting is superseded by the newer one (the person already spoke again).
_SUPERSEDE_AFTER_SECONDS = 1.5
#: Sentence split for pipelined synthesis; keeps the first spoken part short.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")
_MAX_SENTENCE_CHARS = 240

#: Reason texts used in logs and in the call audit record.
_END_REASONS = {
    "remote_hangup": "The other side ended the call.",
    "timeout": "The call timed out.",
    "max_duration": "The call reached its maximum duration.",
    "connection_lost": "The call connection was lost.",
    "owner_hangup": "The call was ended locally.",
    "farewell": "The conversation was over: both sides said goodbye.",
    "failed": "A technical error stopped the call.",
}


def _split_sentences(text: str) -> list[str]:
    """Split a reply into speakable sentences, chunking very long ones."""
    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(text) if part.strip()]
    chunks: list[str] = []
    for part in parts:
        if len(part) <= _MAX_SENTENCE_CHARS:
            chunks.append(part)
            continue
        current = ""
        for word in part.split():
            if current and len(current) + len(word) + 1 > _MAX_SENTENCE_CHARS:
                chunks.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            chunks.append(current)
    return chunks


def _speakable_sentences(text: str) -> list[str]:
    """Drop sentences that are not Uzbek before they reach the voice.

    The model is told to answer in Uzbek and the brain rewrites a reply that
    came back in another language, so this is the last check, not the first.
    A borrowed name or brand inside an Uzbek sentence still passes; a whole
    Russian or English sentence does not.
    """
    kept: list[str] = []
    for sentence in _split_sentences(text):
        # "Okay, albatta." must not be spoken as English just because of the
        # first word; drop the filler and check the rest.
        cleaned = strip_foreign_openers(sentence)
        if not cleaned:
            logger.info("Telegram call: dropping a foreign filler sentence")
            continue
        sentence = cleaned
        report = language_report(sentence)
        if report.ok:
            kept.append(sentence)
            continue
        logger.warning(
            "Telegram call: skipping a sentence that is not Uzbek (%s, %d chars)",
            report.detail,
            len(sentence),
        )
    return kept


class VoiceCallSession:
    """A single Telegram 1:1 call with end-to-end AI audio."""

    def __init__(
        self,
        settings: Settings,
        transport: Any,
        client: Any,
        *,
        peer_id: int,
        speech: Any,
        on_message: MessageHandler,
        on_finished: FinishedHandler | None = None,
        on_schedule_call: Any | None = None,
        direction: str = "outgoing",
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client = client
        self._peer_id = int(peer_id)
        #: Handler that stores a callback reminder and returns its time.
        self._on_schedule_call = on_schedule_call
        self._speech = speech
        self._on_message = on_message
        self._on_finished = on_finished
        self._direction = direction
        self._protocol: Any | None = None

        self._call_id: int | None = None
        self._access_hash: int | None = None
        self._state = "idle"
        self._started_at = datetime.now(timezone.utc)
        self._connected_at: datetime | None = None
        self._connected = False
        self._closing = False

        #: Inbound ``updatePhoneCallSignalingData`` packets are queued until
        #: the native signaling channel exists (end of ``connect_p2p``).
        self._signaling_ready = False
        self._signaling_backlog: list[bytes] = []
        self._signaling_backlog_bytes = 0
        #: True once the DH exchange / connect pipeline has started. Telegram
        #: re-sends PhoneCall* updates, and starting it twice makes ntgcalls
        #: throw "Key already exchanged" / "Connection already made", which
        #: used to kill an otherwise healthy call.
        self._negotiation_started = False
        #: True while the native transport is not CONNECTED. Frames are read
        #: from the outbox but not handed to a dead connection.
        self._transport_paused = False
        self._last_error: str | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self._inbox: asyncio.Queue[tuple[str, str, bytes]] = asyncio.Queue(maxsize=2000)

        self._pump_task: asyncio.Task[None] | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._respond_task: asyncio.Task[None] | None = None
        self._greet_task: asyncio.Task[None] | None = None
        self._goodbye_task: asyncio.Task[None] | None = None

        #: Farewell handling. A detected goodbye never hangs up directly: it
        #: speaks a short goodbye, waits ``goodbye_grace_seconds`` and only then
        #: ends the call. Any new speech bumps the generation and cancels it.
        self._goodbye_pending = False
        self._goodbye_generation = 0
        self._goodbyes = 0
        self._goodbye_cancelled = 0

        self._utterances = 0
        self._replies = 0
        self._interruptions = 0
        #: Diagnostics: counts only (no packet contents).
        self._signaling_rx = 0
        self._signaling_fed = 0
        self._frames_tx = 0
        self._frames_rx = 0
        self._connect_started_at: float | None = None
        self._speaking = False

        #: One worker answers utterances strictly in order; the backlog is
        #: bounded and never cancels the reply that is already in flight.
        self._utterance_queue: deque[tuple[float, bytes]] = deque(
            maxlen=_MAX_PENDING_UTTERANCES
        )
        self._utterance_started = 0
        self._utterance_dropped = 0
        self._utterance_stale = 0
        #: Bumped on every barge-in so a reply already being queued stops.
        self._speech_generation = 0
        self._playback_started_at = 0.0
        self._utterance_superseded = 0
        self._stt_cache: OrderedDict[str, str] = OrderedDict()
        self._last_transcript: str | None = None
        self._last_transcript_at = 0.0

        #: Database row id, set by the manager when the call is recorded.
        self.record_id: int | None = None

    # -- state ------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        return self._state in {"dialing", "answered", "connecting", "active", "recovering"}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def call_id(self) -> int | None:
        return self._call_id

    @property
    def access_hash(self) -> int | None:
        return self._access_hash

    @property
    def started_at(self) -> datetime:
        return self._started_at

    @property
    def peer_id(self) -> int:
        return self._peer_id

    @property
    def direction(self) -> str:
        return self._direction

    def matches(self, call: Any) -> bool:
        """True when a phone-call update belongs to this session."""
        call_id = getattr(call, "id", None)
        return call_id is None or self._call_id is None or int(call_id) == self._call_id

    def status(self) -> dict[str, Any]:
        return {
            "direction": self._direction,
            "peer_id": self._peer_id,
            "call_id": self._call_id,
            "state": self._state,
            "connected": self._connected,
            "connected_seconds": self._connected_seconds(),
            "utterances": self._utterances,
            "utterances_dropped": self._utterance_dropped,
            "utterances_stale": self._utterance_stale,
            "utterances_superseded": self._utterance_superseded,
            "replies": self._replies,
            "interruptions": self._interruptions,
            "goodbye_pending": self._goodbye_pending,
            "goodbyes": self._goodbyes,
            "goodbyes_cancelled": self._goodbye_cancelled,
            "error": self._last_error,
            "signaling_received": self._signaling_rx,
            "signaling_delivered": self._signaling_fed,
            "frames_sent": self._frames_tx,
            "frames_received": self._frames_rx,
        }

    def _connected_seconds(self) -> float:
        if self._connected_at is None:
            return 0.0
        return round((datetime.now(timezone.utc) - self._connected_at).total_seconds(), 1)

    # -- outgoing --------------------------------------------------------- #
    async def place(self) -> tuple[bool, str]:
        """Dial the owner. Never pools money into random calls: one attempt."""
        self._loop = asyncio.get_running_loop()
        self._direction = "outgoing"
        self._state = "dialing"
        try:
            peer = await self._client.get_input_entity(self._peer_id)
            await self._transport.prepare(self._peer_id)
            dh = await self._dh_config()
            g_a_hash = await self._transport.init_exchange(
                self._peer_id, dh.g, dh.p, dh.random, None
            )
            self._protocol = build_phone_call_protocol()
            result = await self._client(
                functions.phone.RequestCallRequest(
                    user_id=peer,
                    random_id=random.getrandbits(31),
                    g_a_hash=g_a_hash,
                    protocol=self._protocol,
                    video=False,
                )
            )
            call = getattr(result, "phone_call", None)
            if call is None:
                raise MediaError("Telegram did not return a call object.")
            self._call_id = int(call.id)
            self._access_hash = int(call.access_hash)
            self._state = "answered"
            self._arm_timeout(
                self._settings.telegram_call_ring_timeout_seconds, "ring timeout"
            )
            logger.info(
                "Telegram call: dialing owner (call_id=%s, frames=10ms/48kHz)",
                self._call_id,
            )
            return True, "Calling you on Telegram now. Pick up to talk."
        except Exception as exc:  # noqa: BLE001 - network/signaling failures are expected
            self._state = "failed"
            self._last_error = type(exc).__name__
            logger.error(
                "Telegram call: could not start (reason=%s)", type(exc).__name__, exc_info=True
            )
            await self._cleanup_transport()
            return False, f"I could not start the Telegram call ({type(exc).__name__})."

    # -- incoming --------------------------------------------------------- #
    async def accept(self, requested: Any) -> tuple[bool, str]:
        """Answer an incoming ``PhoneCallRequested`` from the owner."""
        self._loop = asyncio.get_running_loop()
        self._direction = "incoming"
        self._state = "connecting"
        self._call_id = int(requested.id)
        self._access_hash = int(requested.access_hash)
        try:
            await self._transport.prepare(self._peer_id)
            dh = await self._dh_config()
            g_b = await self._transport.init_exchange(
                self._peer_id, dh.g, dh.p, dh.random, bytes(requested.g_a_hash)
            )
            self._protocol = build_phone_call_protocol()
            peer = types.InputPhoneCall(id=self._call_id, access_hash=self._access_hash)
            result = await self._client(
                functions.phone.AcceptCallRequest(
                    peer=peer, g_b=g_b, protocol=self._protocol
                )
            )
            call = getattr(result, "phone_call", None)
            if call is None:
                raise MediaError("Telegram did not return a call object.")
            self._state = "answered"
            self._arm_timeout(
                self._settings.telegram_call_connect_timeout_seconds, "connect timeout"
            )
            logger.info(
                "Telegram call: answered incoming call (call_id=%s)", self._call_id
            )
            return True, "Incoming Telegram call answered."
        except Exception as exc:  # noqa: BLE001
            self._state = "failed"
            self._last_error = type(exc).__name__
            logger.error(
                "Telegram call: could not answer (reason=%s)", type(exc).__name__, exc_info=True
            )
            await self._cleanup_transport()
            await self._notify_finished()
            return False, f"I could not answer the Telegram call ({type(exc).__name__})."

    # -- signaling -------------------------------------------------------- #
    async def handle_phone_call(self, call: Any) -> None:
        """Apply a ``PhoneCall*`` state update from Telegram."""
        name = type(call).__name__
        if self._call_id is None and getattr(call, "id", None) is not None:
            self._call_id = int(call.id)
        if self._access_hash is None and getattr(call, "access_hash", None) is not None:
            self._access_hash = int(call.access_hash)

        try:
            if name == "PhoneCallAccepted" and self._direction == "outgoing":
                await self._on_accepted(call)
            elif name == "PhoneCall" and not self._connected:
                await self._on_confirmed(call)
            elif name == "PhoneCallDiscarded":
                reason = type(getattr(call, "reason", None)).__name__ or "discarded"
                logger.info("Telegram call: remote ended the call (reason=%s)", reason)
                await self._finish("remote_hangup", detail=reason, discard=False)
        except Exception as exc:  # noqa: BLE001 - signaling errors must not crash the client
            self._last_error = type(exc).__name__
            logger.error(
                "Telegram call: signaling step failed (%s)", name, exc_info=True
            )
            await self._finish("failed", detail=type(exc).__name__)

    async def handle_signaling(self, data: bytes) -> None:
        """Feed ``updatePhoneCallSignalingData`` into the native transport.

        The peer's initial setup (ufrag/pwd/fingerprints) and its first ICE
        candidates arrive right after ``acceptCall``/``confirmCall`` and can
        beat ``connect_p2p``. ntgcalls can only accept them once the call
        exists, so they are queued here and replayed in order instead of being
        dropped: losing the initial setup means ICE never starts, the
        transport reports TIMEOUT after 10 seconds and the call dies.
        """
        if not data:
            return
        self._signaling_rx += 1
        if self._signaling_rx == 1:
            logger.info(
                "Telegram call: first signaling packet from Telegram (%d bytes)", len(data)
            )
        if not self._signaling_ready:
            self._queue_signaling(data)
            return
        await self._feed_signaling(data)

    def _queue_signaling(self, data: bytes) -> None:
        if (
            len(self._signaling_backlog) >= _MAX_SIGNALING_BACKLOG_PACKETS
            or self._signaling_backlog_bytes + len(data) > _MAX_SIGNALING_BACKLOG_BYTES
        ):
            logger.warning("Telegram call: signaling backlog is full; dropping a packet")
            return
        self._signaling_backlog.append(data)
        self._signaling_backlog_bytes += len(data)

    async def _feed_signaling(self, data: bytes) -> None:
        try:
            await self._transport.feed_signaling(self._peer_id, data)
            self._signaling_fed += 1
        except Exception:  # noqa: BLE001 - ICE retries must not kill the call
            logger.warning(
                "Telegram call: signaling packet rejected by the transport (%s)",
                "native",
            )

    async def _activate_signaling(self) -> None:
        """Replay queued signaling once ``connect_p2p`` created the channel.

        Packets that arrive while the backlog is being replayed append to it
        and are drained by the loop, so the peer's setup stays first while
        ordering inside one batch is preserved.
        """
        while self._signaling_backlog:
            batch = self._signaling_backlog
            self._signaling_backlog = []
            self._signaling_backlog_bytes = 0
            for packet in batch:
                await self._feed_signaling(packet)
        self._signaling_ready = True

    async def _on_accepted(self, call: Any) -> None:
        if self._negotiation_started:
            logger.debug("Telegram call: duplicate PhoneCallAccepted ignored")
            return
        self._negotiation_started = True
        auth = await self._transport.exchange_keys(
            self._peer_id, bytes(call.g_b), int(getattr(call, "key_fingerprint", 0) or 0)
        )
        peer = types.InputPhoneCall(id=self._call_id, access_hash=self._access_hash)
        result = await self._client(
            functions.phone.ConfirmCallRequest(
                peer=peer,
                g_a=bytes(auth.g_a_or_b),
                key_fingerprint=int(auth.key_fingerprint),
                protocol=self._protocol,
            )
        )
        confirmed = getattr(result, "phone_call", None)
        await self._connect_transport(confirmed if confirmed is not None else call)

    async def _on_confirmed(self, call: Any) -> None:
        if self._negotiation_started:
            logger.debug("Telegram call: duplicate PhoneCall ignored; negotiation already ran")
            return
        self._negotiation_started = True
        g_a_or_b = getattr(call, "g_a_or_b", None)
        if g_a_or_b:
            await self._transport.exchange_keys(
                self._peer_id,
                bytes(g_a_or_b),
                int(getattr(call, "key_fingerprint", 0) or 0),
            )
        await self._connect_transport(call)

    async def _connect_transport(self, call: Any) -> None:
        connections = list(getattr(call, "connections", None) or [])
        if not connections:
            raise MediaError("Telegram returned no call connection endpoints.")
        # The signaling protocol version must come from the peer's advertised
        # PhoneCallProtocol: ntgcalls selects the highest version in the list
        # it supports (8/9 = V2, 12/13 = V3). Using our own list forces V3 and
        # the peer's V2 handshake never starts (10 s TIMEOUT / Disconnect).
        peer_protocol = getattr(call, "protocol", None)
        peer_versions = getattr(peer_protocol, "library_versions", None)
        versions = [str(item) for item in peer_versions] if peer_versions else None
        logger.info(
            "Telegram call: connecting transport (peer_versions=%s, endpoints=%d)",
            versions or "none",
            len(connections),
        )
        self._connect_started_at = asyncio.get_running_loop().time()
        await self._transport.connect(
            self._peer_id,
            connections,
            p2p_allowed=bool(getattr(call, "p2p_allowed", True)),
            custom_parameters=getattr(call, "custom_parameters", None),
            versions=versions,
        )
        # connect_p2p creates the native signaling channel, so this is the
        # earliest point where the peer's queued setup/ICE can be delivered.
        await self._activate_signaling()
        await self._transport.start_playback(self._peer_id)
        self._connected = True
        self._transport_paused = False
        self._connected_at = datetime.now(timezone.utc)
        self._state = "active"
        self._cancel_timeout()
        self._pump_task = asyncio.create_task(self._pump_audio(), name="call-audio-out")
        self._listen_task = asyncio.create_task(self._listen_audio(), name="call-audio-in")
        self._watchdog_task = asyncio.create_task(
            self._watch_duration(), name="call-watchdog"
        )
        logger.info(
            "Telegram call: audio path active (48 kHz mono, 10 ms frames, direction=%s)",
            self._direction,
        )
        self._greet_task = asyncio.create_task(self._greet(), name="call-greeting")

    # -- audio in --------------------------------------------------------- #
    def push_frames(self, chat_id: int, mode: str, device: str, data: bytes) -> None:
        """Called from the native thread with inbound PCM frames."""
        loop = self._loop
        if loop is None or self._closing or not data:
            return
        self._frames_rx += 1
        # Register the chat_id with the transport on first inbound frame
        # This is the ntgcalls connection identifier, which may differ from peer_id
        if hasattr(self._transport, 'register_connection_chat_id'):
            self._transport.register_connection_chat_id(self._peer_id, chat_id)
        loop.call_soon_threadsafe(self._enqueue_inbound, mode, device, data)

    def _enqueue_inbound(self, mode: str, device: str, data: bytes) -> None:
        try:
            self._inbox.put_nowait((mode, device, data))
        except asyncio.QueueFull:
            # Drop the oldest frame instead of growing latency without bound.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._inbox.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._inbox.put_nowait((mode, device, data))

    async def _listen_audio(self) -> None:
        """Voice-activity detection over inbound frames."""
        threshold = self._settings.telegram_call_energy_threshold
        silence_ms = self._settings.telegram_call_silence_ms
        min_speech_ms = self._settings.telegram_call_min_speech_ms
        barge_threshold = (
            threshold * self._settings.telegram_call_barge_in_threshold_scale
        )
        barge_frames_needed = max(
            1, round(self._settings.telegram_call_barge_in_ms / FRAME_DURATION_MS)
        )
        buffer = bytearray()
        speaking = False
        silence_run = 0
        barge_run = 0
        max_buffer = BYTES_PER_FRAME * 3000  # 30 seconds hard cap

        try:
            while not self._closing:
                mode, _device, data = await self._inbox.get()
                if mode != "PLAYBACK":
                    continue

                if self._speaking:
                    # Our own voice can come back through the caller's mic.
                    # Only sustained, louder speech interrupts; echo, clicks
                    # and background noise are ignored without buffering them.
                    playing_ms = (time.monotonic() - self._playback_started_at) * 1000
                    within_grace = (
                        playing_ms < self._settings.telegram_call_barge_in_grace_ms
                    )
                    loud = is_speech(data, barge_threshold) and not within_grace
                    barge_run = barge_run + 1 if loud else 0
                    if barge_run < barge_frames_needed:
                        continue
                    logger.info(
                        "Telegram call: barge-in accepted after %d ms of speech "
                        "(threshold %.0f, playing %.0f ms)",
                        barge_run * FRAME_DURATION_MS,
                        barge_threshold,
                        playing_ms,
                    )
                    barge_run = 0
                    self._barge_in()

                speech = is_speech(data, threshold)
                if speech:
                    speaking = True
                    silence_run = 0
                    buffer.extend(data)
                elif speaking:
                    silence_run += FRAME_DURATION_MS
                    buffer.extend(data)
                    if silence_run >= silence_ms:
                        pcm = bytes(buffer)
                        buffer.clear()
                        voiced_ms = pcm_duration_ms(pcm) - silence_run
                        if voiced_ms >= min_speech_ms:
                            self._schedule_response(pcm)
                        speaking = False
                        silence_run = 0

                if len(buffer) > max_buffer:
                    logger.warning("Telegram call: utterance exceeded 30s; dropped")
                    buffer.clear()
                    speaking = False
                    silence_run = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the listener die silently
            logger.exception("Telegram call: inbound audio loop failed")
            await self._finish("failed", detail="audio-loop")

    # -- audio out -------------------------------------------------------- #
    async def _pump_audio(self) -> None:
        """Send one 10 ms frame every 10 ms, silence when idle."""
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        try:
            while not self._closing:
                try:
                    frame = self._outbox.get_nowait()
                except asyncio.QueueEmpty:
                    frame = SILENCE_FRAME
                    self._speaking = False
                if not self._transport_paused:
                    try:
                        await self._transport.send_frame(self._peer_id, frame)
                        self._frames_tx += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - report once, then stop
                        await self._on_send_failure(exc)
                        return
                next_at += FRAME_DURATION_MS / 1000
                delay = next_at - loop.time()
                if delay <= 0:
                    # Fell behind: resynchronise instead of bursting frames.
                    next_at = loop.time()
                    delay = 0
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Telegram call: outbound audio loop failed")
            await self._finish("failed", detail="audio-loop")

    async def _on_send_failure(self, exc: BaseException) -> None:
        """Classify a failed frame send instead of retrying every 10 ms.

        Once the native call is gone, every further frame raises
        ``ConnectionNotFound``; that is a lost connection, not an audio bug,
        so it is reported once and the session ends cleanly.
        """
        name = type(exc).__name__
        self._last_error = name
        if name in _CONNECTION_LOST_ERRORS:
            logger.warning(
                "Telegram call: outbound audio stopped; the native connection "
                "is gone (%s)",
                name,
            )
            await self._finish("connection_lost", detail=name)
            return
        logger.error("Telegram call: outbound audio failed (%s)", name)
        await self._finish("failed", detail="audio-loop")

    def _push_outbound(self, frame: bytes) -> None:
        if len(frame) != BYTES_PER_FRAME:
            return
        if self._outbox.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._outbox.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._outbox.put_nowait(frame)

    def _flush_outbox(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _barge_in(self) -> None:
        """The caller really started speaking: stop our own speech."""
        if self._outbox.empty() and not self._speaking:
            return
        self._cancel_goodbye("the caller spoke while the goodbye was playing")
        self._flush_outbox()
        self._speaking = False
        # Stop a reply that is still streaming frames into the queue.
        self._speech_generation += 1
        self._interruptions += 1
        logger.info("Telegram call: caller interrupted, outbound speech flushed")

    # -- conversation ----------------------------------------------------- #
    async def _greet(self) -> None:
        if not self._speech.configured:
            logger.warning("Telegram call: speech is not configured; no greeting")
            return
        try:
            await self._speak("Salom! Men Ksenus. Sizni eshityapman.")
        except Exception as exc:  # noqa: BLE001 - a failed greeting must not end the call
            # No traceback: provider errors can echo key fragments into the log.
            logger.warning(
                "Telegram call: greeting could not be spoken (%s)", type(exc).__name__
            )

    def _schedule_response(self, pcm: bytes) -> None:
        """Queue one utterance; a single worker answers them in order.

        Cancelling the reply in flight (the previous behaviour) lost that
        answer, made the next task return early because the guard flag was
        still set, and let every further VAD chunk start another doomed task.
        The queue keeps the order, bounds the backlog and survives a failed
        STT/LLM/TTS stage.
        """
        if len(self._utterance_queue) == self._utterance_queue.maxlen:
            self._utterance_dropped += 1
            logger.warning(
                "Telegram call: utterance backlog full (%d); dropping the oldest "
                "stale utterance",
                self._utterance_queue.maxlen,
            )
        # New speech always wins over a goodbye that is waiting to hang up.
        self._cancel_goodbye("the caller said something new")
        self._utterance_queue.append((time.monotonic(), pcm))
        if self._respond_task is None or self._respond_task.done():
            self._respond_task = asyncio.create_task(
                self._respond_worker(), name="call-response"
            )

    async def _respond_worker(self) -> None:
        """Answer queued utterances one by one; a failure never blocks the queue."""
        try:
            while self._utterance_queue and not self._closing:
                enqueued_at, pcm = self._utterance_queue.popleft()
                waited = time.monotonic() - enqueued_at
                if waited > self._settings.telegram_call_stale_utterance_seconds:
                    # Answering this late would be confusing on a live call.
                    self._utterance_stale += 1
                    logger.warning(
                        "Telegram call: skipping stale utterance (waited %.1fs)",
                        waited,
                    )
                    continue
                if self._utterance_queue and waited > _SUPERSEDE_AFTER_SECONDS:
                    # The person kept talking: answer the newer utterance
                    # instead of the old one that is already out of date.
                    self._utterance_superseded += 1
                    logger.info(
                        "Telegram call: utterance superseded by a newer one "
                        "(waited %.1fs)",
                        waited,
                    )
                    continue
                self._utterance_started += 1
                index = self._utterance_started
                started = time.monotonic()
                try:
                    await self._respond(pcm, index)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never stop the conversation
                    logger.error(
                        "Telegram call: utterance #%d failed (%s); continuing",
                        index,
                        type(exc).__name__,
                    )
                finally:
                    logger.info(
                        "Telegram call: utterance #%d: pipeline finished in %.2fs "
                        "(queue wait %.2fs)",
                        index,
                        time.monotonic() - started,
                        waited,
                    )
        finally:
            self._respond_task = None
            if self._utterance_queue and not self._closing:
                self._respond_task = asyncio.create_task(
                    self._respond_worker(), name="call-response"
                )

    async def _respond(self, pcm: bytes, index: int = 0) -> None:
        """STT -> LLM -> TTS for one utterance, logging each stage separately."""
        loop = asyncio.get_running_loop()

        # -- STT --------------------------------------------------------- #
        digest = hashlib.sha1(pcm).hexdigest()
        transcript = self._stt_cache.get(digest)
        if transcript is not None:
            logger.info(
                "Telegram call: utterance #%d: STT skipped, reusing transcript (%d chars)",
                index,
                len(transcript),
            )
        else:
            logger.info(
                "Telegram call: utterance #%d: STT started (%.0f ms audio)",
                index,
                pcm_duration_ms(pcm),
            )
            started = loop.time()
            try:
                wav = pcm16_to_wav(pcm)
                # ``stt_language_hint`` is None unless STT_LANGUAGE is set, so
                # the provider keeps auto-detecting: the person may speak
                # Russian or English and still get an Uzbek answer.
                transcript = (
                    await self._speech.transcribe(
                        wav,
                        filename="call.wav",
                        language=self._settings.stt_language_hint,
                    )
                ).strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the call must stay up
                logger.error(
                    "Telegram call: utterance #%d: STT failed after %.2fs (%s)",
                    index,
                    loop.time() - started,
                    type(exc).__name__,
                )
                return
            logger.info(
                "Telegram call: utterance #%d: STT done in %.2fs (%d chars)",
                index,
                loop.time() - started,
                len(transcript),
            )
            self._remember_transcript(digest, transcript)

        if not transcript:
            logger.info("Telegram call: utterance #%d: no speech in the audio", index)
            return
        if self._is_repeat(transcript):
            logger.info(
                "Telegram call: utterance #%d: duplicate transcript ignored", index
            )
            return
        self._utterances += 1
        logger.info("Telegram call: heard the owner (%d characters)", len(transcript))

        # -- "call me back in N minutes" --------------------------------- #
        # Scheduled here, in code, so it does not depend on the model choosing
        # a tool: the person asked for a call at a time, and the current call
        # is over either way.
        callback_minutes = callback_delay_minutes(transcript)
        if (
            callback_minutes is not None
            and self._on_schedule_call is not None
            and not self._closing
        ):
            await self._schedule_callback(callback_minutes)
            return

        # -- farewell ---------------------------------------------------- #
        # A goodbye is answered with a goodbye, not with a model turn: the
        # person is leaving, so there is nothing to think about.
        farewell = farewell_match(transcript)
        if farewell is not None and not self._closing:
            self._begin_goodbye(farewell)
            return

        # -- LLM --------------------------------------------------------- #
        logger.info("Telegram call: utterance #%d: LLM started", index)
        started = loop.time()
        try:
            reply = await self._on_message(transcript)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed reply must not kill the call
            logger.error(
                "Telegram call: utterance #%d: LLM failed after %.2fs (%s: %s)",
                index,
                loop.time() - started,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return
        text = reply.strip() if isinstance(reply, str) else ""
        if not text:
            # OpenRouter can answer HTTP 200 with an empty message (reasoning
            # models, tool-only turns). Nothing is spoken, but the call lives.
            logger.warning(
                "Telegram call: utterance #%d: LLM returned no text after %.2fs",
                index,
                loop.time() - started,
            )
            return
        logger.info(
            "Telegram call: utterance #%d: LLM done in %.2fs (%d chars)",
            index,
            loop.time() - started,
            len(text),
        )
        self._replies += 1

        # -- TTS --------------------------------------------------------- #
        logger.info(
            "Telegram call: utterance #%d: TTS started (%d chars)", index, len(text)
        )
        started = loop.time()
        try:
            frames = await self._speak(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - quota/network errors stay per utterance
            logger.warning(
                "Telegram call: utterance #%d: TTS failed after %.2fs (%s)",
                index,
                loop.time() - started,
                type(exc).__name__,
            )
            return
        logger.info(
            "Telegram call: utterance #%d: TTS done in %.2fs (%d frames)",
            index,
            loop.time() - started,
            frames,
        )

    def _remember_transcript(self, digest: str, transcript: str) -> None:
        if not transcript:
            return
        self._stt_cache[digest] = transcript
        self._stt_cache.move_to_end(digest)
        while len(self._stt_cache) > _MAX_STT_CACHE:
            self._stt_cache.popitem(last=False)

    def _is_repeat(self, transcript: str) -> bool:
        """True when VAD split one sentence into two identical utterances."""
        now = time.monotonic()
        normalized = " ".join(transcript.lower().split())
        if (
            self._last_transcript is not None
            and normalized == self._last_transcript
            and now - self._last_transcript_at <= _TRANSCRIPT_REPEAT_WINDOW_SECONDS
        ):
            return True
        self._last_transcript = normalized
        self._last_transcript_at = now
        return False

    # -- ending the conversation politely --------------------------------- #
    def _begin_goodbye(self, farewell: Farewell) -> None:
        """Start the goodbye flow: speak, wait, then end the call cleanly.

        Never hangs up here. One flow at a time; a second farewell while the
        first one is still waiting is ignored, so the call cannot be ended
        twice or be interrupted by its own goodbye.
        """
        if self._goodbye_pending:
            logger.info(
                "Telegram call: farewell %s ignored, a goodbye is already running",
                farewell.detail,
            )
            return
        self._goodbye_pending = True
        self._goodbyes += 1
        self._goodbye_generation += 1
        logger.info(
            "Telegram call: farewell detected (%s); saying goodbye before hanging up",
            farewell.detail,
        )
        self._goodbye_task = asyncio.create_task(
            self._goodbye_flow(self._goodbye_generation, farewell),
            name="call-goodbye",
        )

    async def _goodbye_flow(self, generation: int, farewell: Farewell) -> None:
        """Speak the goodbye, give the caller a chance to continue, then hang up."""
        try:
            await self._speak(farewell.goodbye)
            if generation != self._goodbye_generation or self._closing:
                return  # cancelled while speaking
            await asyncio.sleep(self._settings.telegram_call_goodbye_grace_seconds)
            if generation != self._goodbye_generation:
                logger.info(
                    "Telegram call: goodbye cancelled; the conversation continues"
                )
                return
            if self._closing:
                return
            self._goodbye_pending = False
            logger.info(
                "Telegram call: nothing more was said after the goodbye; ending the call"
            )
            await self.hangup("farewell")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed goodbye must not kill the call
            self._goodbye_pending = False
            logger.warning(
                "Telegram call: goodbye flow failed (%s); the call stays open",
                type(exc).__name__,
            )

    async def _schedule_callback(self, minutes: int) -> None:
        """Store the callback, confirm it out loud and end this call."""
        detail: str | None = None
        try:
            detail = await self._on_schedule_call(minutes)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed schedule must not kill the call
            logger.exception("Telegram call: could not schedule the callback")
        if not detail:
            logger.warning("Telegram call: callback was requested but not scheduled")
            await self._speak("Kechirasiz, qo‘ng‘iroqni hozir rejalashtira olmadim.")
            return
        logger.info(
            "Telegram call: callback scheduled in %d minutes (%s)", minutes, detail
        )
        await self._speak(
            f"Xo‘p, {minutes} daqiqadan keyin qo‘ng‘iroq qilaman. Sog‘ bo‘ling!"
        )
        await asyncio.sleep(0.4)
        await self.hangup("callback_scheduled")

    def _cancel_goodbye(self, reason: str) -> None:
        """Give up a pending hang-up because the caller is still talking."""
        if not self._goodbye_pending:
            return
        self._goodbye_pending = False
        self._goodbye_cancelled += 1
        self._goodbye_generation += 1
        logger.info("Telegram call: goodbye cancelled (%s)", reason)

    async def _speak(self, text: str) -> int:
        """Speak ``text`` sentence by sentence; returns frames queued.

        The next sentence is synthesised while the current one is playing, so
        the assistant starts talking as soon as the first sentence exists
        instead of waiting for the whole reply. Frames are pushed at playback
        pace and stop immediately when the caller barges in.

        Before anything is synthesised the text is made speakable: markup is
        removed and numbers are rewritten into spoken Uzbek ("15" becomes
        "o‘n besh"), because the voice has no number rules of its own. A
        sentence that is not Uzbek is not spoken at all.
        """
        if not text.strip():
            return 0
        spoken_text = prepare_for_tts(text)
        sentences = _speakable_sentences(spoken_text)
        if not sentences:
            logger.warning(
                "Telegram call: nothing in this reply was Uzbek; staying silent"
            )
            return 0
        left = unread_digits(spoken_text)
        if left:
            logger.warning(
                "Telegram call: %d digit(s) reached TTS unconverted; they are "
                "inside a word or code",
                len(left),
            )

        self._flush_outbox()
        self._speaking = True
        self._playback_started_at = time.monotonic()
        synthesis_started = self._playback_started_at
        generation = self._speech_generation
        queued = 0
        first_frame_at: float | None = None
        pending: deque[asyncio.Task[bytes]] = deque()
        next_index = 0
        spoken_chunks = 0

        try:
            while next_index < len(sentences) or pending:
                # Two sentences of lookahead: enough to hide synthesis while
                # keeping the first sound as early as possible.
                while len(pending) < 2 and next_index < len(sentences):
                    pending.append(
                        asyncio.create_task(
                            self._speech.synthesize(
                                sentences[next_index], response_format="pcm"
                            )
                        )
                    )
                    next_index += 1

                task = pending.popleft()
                try:
                    pcm = await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad sentence is survivable
                    logger.warning(
                        "Telegram call: sentence %d could not be synthesised (%s)",
                        spoken_chunks + 1,
                        type(exc).__name__,
                    )
                    continue
                # Providers pad every request with silence; without trimming,
                # each sentence boundary adds a noticeable dead pause.
                # ``or pcm`` keeps a clip that was silence all along (for
                # example a test double) instead of muting the sentence.
                pcm = trim_silence_edges(pcm or b"") or pcm
                if pcm and spoken_chunks:
                    # A short, natural break between sentences (~60 ms).
                    pcm = b"\x00\x00" * (_SENTENCE_GAP_MS * TTS_SAMPLE_RATE // 1000) + pcm
                spoken_chunks += 1
                frames = split_frames(upsample_24k_to_48k(pcm or b""))
                for frame in frames:
                    if generation != self._speech_generation or self._closing:
                        return queued
                    try:
                        await asyncio.wait_for(
                            self._outbox.put(frame),
                            timeout=_OUTBOUND_PUT_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Telegram call: outbound queue stalled; reply audio cut short"
                        )
                        return queued
                    queued += 1
                    if first_frame_at is None:
                        # Time to first audible frame: the metric the caller feels.
                        first_frame_at = time.monotonic()
                        logger.info(
                            "Telegram call: first audio queued %.2fs after TTS start",
                            first_frame_at - synthesis_started,
                        )
            logger.info(
                "Telegram call: speaking %d chars in %d sentence(s) (%d frames, ~%.1fs)",
                len(text),
                spoken_chunks,
                queued,
                queued * FRAME_DURATION_MS / 1000,
            )
            return queued
        finally:
            for task in pending:
                task.cancel()

    # -- connection changes ----------------------------------------------- #
    def connection_changed(self, chat_id: int, state: str) -> None:
        """Called from the native thread when the transport state changes."""
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._handle_connection_state, state)

    def _handle_connection_state(self, state: str) -> None:
        if self._closing:
            return
        if state == "CONNECTED":
            if self._state == "recovering":
                logger.info("Telegram call: connection recovered")
            logger.info(
                "Telegram call: transport CONNECTED after %.2fs (signaling rx=%d, "
                "delivered=%d, frames tx=%d rx=%d)",
                self._since_connect_seconds(),
                self._signaling_rx,
                self._signaling_fed,
                self._frames_tx,
                self._frames_rx,
            )
            self._state = "active"
            self._transport_paused = False
            return
        if state in {"FAILED", "TIMEOUT", "CLOSED"}:
            # Stop handing frames to a connection that is not up; sending them
            # only produces ConnectionNotFound noise until it recovers.
            self._transport_paused = True
            # This snapshot is what separates a dead signaling relay from a
            # blocked ICE/UDP path: rx==0 means Telegram's packets never
            # arrived, rx>0 with no CONNECTED means the media path failed.
            logger.warning(
                "Telegram call: transport %s after %.2fs (signaling rx=%d, delivered=%d, "
                "frames tx=%d rx=%d, native=%s); waiting up to %ss for recovery",
                state,
                self._since_connect_seconds(),
                self._signaling_rx,
                self._signaling_fed,
                self._frames_tx,
                self._frames_rx,
                self._transport_stats(),
                self._settings.telegram_call_reconnect_grace_seconds,
            )
            self._state = "recovering"
            if self._recovery_task is not None:
                self._recovery_task.cancel()
            self._recovery_task = asyncio.create_task(
                self._await_recovery(state), name="call-recovery"
            )

    def _since_connect_seconds(self) -> float:
        if self._connect_started_at is None:
            return 0.0
        try:
            return asyncio.get_running_loop().time() - self._connect_started_at
        except RuntimeError:  # pragma: no cover - not called off the loop
            return 0.0

    def _transport_stats(self) -> dict[str, Any]:
        stats = getattr(self._transport, "stats", None)
        if stats is None:
            return {}
        try:
            return dict(stats())
        except Exception:  # noqa: BLE001 - diagnostics must never break a call
            return {}

    async def _await_recovery(self, state: str) -> None:
        try:
            await asyncio.sleep(self._settings.telegram_call_reconnect_grace_seconds)
        except asyncio.CancelledError:
            return
        if self._closing or self._state != "recovering":
            return
        logger.warning("Telegram call: no recovery after %s; ending the call", state)
        await self._finish("connection_lost", detail=state)

    # -- timeouts --------------------------------------------------------- #
    def _arm_timeout(self, seconds: int, label: str) -> None:
        self._cancel_timeout()
        self._timeout_task = asyncio.create_task(
            self._timeout_after(max(1, seconds), label), name="call-timeout"
        )

    def _cancel_timeout(self) -> None:
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None

    async def _timeout_after(self, seconds: int, label: str) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if self._connected or self._closing:
            return
        logger.warning("Telegram call: %s after %ss", label, seconds)
        await self._finish("timeout", detail=label, missed=True)

    async def _watch_duration(self) -> None:
        try:
            await asyncio.sleep(self._settings.telegram_call_max_seconds)
        except asyncio.CancelledError:
            return
        if self._closing:
            return
        logger.warning(
            "Telegram call: maximum duration reached (%ss)",
            self._settings.telegram_call_max_seconds,
        )
        await self._finish("max_duration")

    # -- teardown --------------------------------------------------------- #
    async def hangup(self, reason: str = "owner_hangup") -> tuple[bool, str]:
        if self._call_id is None:
            return False, "There is no active Telegram call."
        await self._finish(reason, discard=True)
        return True, "The Telegram call was ended."

    async def _finish(
        self,
        reason: str,
        *,
        detail: str | None = None,
        discard: bool = True,
        missed: bool = False,
    ) -> None:
        if self._closing:
            return
        self._closing = True
        duration = self._connected_seconds()
        state_before = self._state
        self._state = "ended"

        current = asyncio.current_task()
        tasks = [
            task
            for task in (
                self._pump_task,
                self._listen_task,
                self._watchdog_task,
                self._timeout_task,
                self._recovery_task,
                self._respond_task,
                self._greet_task,
                self._goodbye_task,
            )
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        self._pump_task = self._listen_task = self._watchdog_task = None
        self._timeout_task = self._recovery_task = self._respond_task = None
        self._greet_task = None
        self._goodbye_task = None
        self._goodbye_pending = False
        self._utterance_queue.clear()
        # Await the cancellations before the native call is stopped, otherwise
        # a frame in flight is sent to an already stopped connection and
        # raises ConnectionNotFound during teardown.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if discard and self._call_id is not None and self._access_hash is not None:
            reason_class = (
                types.PhoneCallDiscardReasonMissed
                if missed
                else types.PhoneCallDiscardReasonHangup
            )
            try:
                await self._client(
                    functions.phone.DiscardCallRequest(
                        peer=types.InputPhoneCall(
                            id=self._call_id, access_hash=self._access_hash
                        ),
                        duration=int(duration),
                        reason=reason_class(),
                        connection_id=0,
                        video=False,
                    )
                )
            except Exception:  # noqa: BLE001 - already-discarded calls are normal
                logger.debug("Telegram call: discard request failed", exc_info=True)

        await self._cleanup_transport()
        logger.info(
            "Telegram call: ended (reason=%s, detail=%s, state=%s, "
            "duration=%.1fs, utterances=%d, replies=%d, interruptions=%d)",
            reason,
            detail or "-",
            state_before,
            duration,
            self._utterances,
            self._replies,
            self._interruptions,
        )
        await self._notify_finished(reason, detail or "")

    async def _cleanup_transport(self) -> None:
        try:
            await self._transport.stop(self._peer_id)
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.debug("Telegram call: transport stop failed", exc_info=True)

    async def _notify_finished(self, reason: str = "", detail: str = "") -> None:
        if self._on_finished is None:
            return
        try:
            result = self._on_finished(self, reason or self._state, detail)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - recording must not break teardown
            logger.exception("Telegram call: completion handler failed")

    # -- helpers ---------------------------------------------------------- #
    async def _dh_config(self) -> Any:
        result = await self._client(
            functions.messages.GetDhConfigRequest(version=0, random_length=256)
        )
        if not hasattr(result, "g") or not hasattr(result, "p"):
            raise MediaError("Telegram did not return a Diffie-Hellman config.")
        return result

    def end_reason_text(self, reason: str) -> str:
        return _END_REASONS.get(reason, reason)
