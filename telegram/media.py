"""Native Telegram call transport backed by ntgcalls 3.x.

`ntgcalls` owns the WebRTC/SRTP side of a private Telegram call: the
Diffie-Hellman exchange, the encrypted UDP transport and the Opus codec. This
adapter is the only place that touches the native module, so the rest of the
project can be tested without it.

Verified against ntgcalls 3.0.7 (`cp314-win_amd64`):

* every call action method returns an awaitable and must be awaited;
* ``create_p2p_call(user_id)`` registers the call, then stream sources are set;
* ``init_exchange(user_id, DhConfig, ga_hash)`` returns the g_a_hash for
  ``phone.requestCall`` when ``ga_hash`` is None, otherwise it returns our g_b;
* ``exchange_keys(user_id, g_a_or_b, fingerprint)`` returns ``AuthParams``
  whose ``g_a_or_b``/``key_fingerprint`` go into ``phone.confirmCall``;
* ``connect_p2p(user_id, servers, versions, p2p_allowed, custom_parameters)``
  takes five arguments in 3.x (older docs list a seventh pair - that is wrong);
* ``MediaDescription`` accepts only four positional arguments;
* inbound frames arrive after PLAYBACK is configured with an EXTERNAL source
  on the microphone slot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from config import Settings
from telegram.audio import BYTES_PER_FRAME, CALL_SAMPLE_RATE

logger = logging.getLogger(__name__)

FrameHandler = Callable[[int, str, str, bytes], None]
SignalingHandler = Callable[[int, bytes], None]
ConnectionHandler = Callable[[int, str], None]


class MediaError(Exception):
    """The native call transport is unavailable or rejected an operation."""


@dataclass(frozen=True)
class ProtocolInfo:
    """What the installed native library supports."""

    library_versions: tuple[str, ...]
    min_layer: int
    max_layer: int
    udp_p2p: bool
    udp_reflector: bool


def load_ntgcalls() -> Any | None:
    """Import the native module, returning None when it is not installed."""
    try:
        import ntgcalls
    except Exception:  # noqa: BLE001 - a missing native wheel is expected
        return None
    return ntgcalls


def protocol_info(module: Any | None = None) -> ProtocolInfo | None:
    """Read the protocol layers from the installed library."""
    module = module or load_ntgcalls()
    if module is None:
        return None
    try:
        protocol = module.NTgCalls.get_protocol()
        return ProtocolInfo(
            library_versions=tuple(str(item) for item in protocol.library_versions),
            min_layer=int(protocol.min_layer),
            max_layer=int(protocol.max_layer),
            udp_p2p=bool(protocol.udp_p2p),
            udp_reflector=bool(protocol.udp_reflector),
        )
    except Exception:  # noqa: BLE001 - introspection must never raise
        logger.exception("Could not read the ntgcalls protocol")
        return None


def build_phone_call_protocol(module: Any | None = None) -> Any:
    """Build the Telethon ``PhoneCallProtocol`` from the native library."""
    from telethon.tl import types

    info = protocol_info(module)
    if info is None:
        raise MediaError("The native call transport is not installed.")
    return types.PhoneCallProtocol(
        min_layer=info.min_layer,
        max_layer=info.max_layer,
        library_versions=list(info.library_versions),
        udp_p2p=info.udp_p2p,
        udp_reflector=info.udp_reflector,
    )


def build_rtc_servers(
    connections: Iterable[Any], module: Any | None = None
) -> list[Any]:
    """Map Telethon ``PhoneConnection*`` objects to ``ntgcalls.RTCServer``.

    The mapping matches pytgcalls' own bridge: Webrtc connections keep their
    TURN/STUN credentials, legacy relay connections authenticate with the peer
    tag and are always treated as TURN endpoints.
    """
    module = module or load_ntgcalls()
    if module is None:
        raise MediaError("The native call transport is not installed.")

    servers: list[Any] = []
    for connection in connections:
        name = type(connection).__name__
        if name == "PhoneConnectionWebrtc":
            servers.append(
                module.RTCServer(
                    id=int(connection.id),
                    ipv4=str(getattr(connection, "ip", "") or ""),
                    ipv6=str(getattr(connection, "ipv6", "") or ""),
                    port=int(connection.port),
                    username=str(getattr(connection, "username", "") or ""),
                    password=str(getattr(connection, "password", "") or ""),
                    turn=bool(getattr(connection, "turn", False)),
                    stun=bool(getattr(connection, "stun", False)),
                    tcp=False,
                    peer_tag=b"",
                )
            )
            continue
        servers.append(
            module.RTCServer(
                id=int(connection.id),
                ipv4=str(getattr(connection, "ip", "") or ""),
                ipv6=str(getattr(connection, "ipv6", "") or ""),
                port=int(connection.port),
                username="",
                password="",
                turn=True,
                stun=False,
                tcp=bool(getattr(connection, "tcp", False)),
                peer_tag=bytes(getattr(connection, "peer_tag", b"") or b""),
            )
        )
    return servers


class NtgTransport:
    """Async wrapper around one ``ntgcalls.NTgCalls`` instance."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ntg: Any | None = None
        self._module: Any | None = None
        self._frame_handler: FrameHandler | None = None
        self._signaling_handler: SignalingHandler | None = None
        self._connection_handler: ConnectionHandler | None = None
        # Map from user_id (peer_id) to ntgcalls chat_id for the active connection
        self._connection_chat_ids: dict[int, int] = {}
        # Diagnostics: counts only, never packet contents or credentials.
        self._frames_sent = 0
        self._frames_received = 0
        self._signaling_sent = 0
        self._signaling_received = 0
        self._last_state: str | None = None

    # -- state ------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        if self._ntg is not None:
            return True
        return load_ntgcalls() is not None

    @property
    def version(self) -> str:
        module = self._module or load_ntgcalls()
        return str(getattr(module, "__version__", "unknown"))

    @property
    def protocol(self) -> ProtocolInfo | None:
        return protocol_info(self._module)

    def devices(self) -> dict[str, list[str]]:
        """Available microphones and speakers, for diagnostics only."""
        module = load_ntgcalls()
        if module is None:
            return {"microphones": [], "speakers": []}
        try:
            devices = module.NTgCalls.get_media_devices()
            return {
                "microphones": [str(item.name) for item in devices.microphone],
                "speakers": [str(item.name) for item in devices.speaker],
            }
        except Exception:  # noqa: BLE001 - diagnostics must not raise
            logger.debug("Could not list audio devices", exc_info=True)
            return {"microphones": [], "speakers": []}

    # -- lifecycle -------------------------------------------------------- #
    def _ensure(self) -> Any:
        if self._ntg is not None:
            return self._ntg
        module = load_ntgcalls()
        if module is None:
            raise MediaError("The native call transport is not installed.")
        try:
            rtc = module.NTgCalls()
            rtc.on_frames(self._emit_frames)
            rtc.on_signaling_data(self._emit_signaling)
            rtc.on_connection_change(self._emit_connection)
        except Exception as exc:  # noqa: BLE001 - native init can fail
            raise MediaError(f"Could not create the native call transport: {exc}") from exc
        self._module = module
        self._ntg = rtc
        return rtc

    def on_frames(self, handler: FrameHandler) -> None:
        self._frame_handler = handler

    def on_signaling_data(self, handler: SignalingHandler) -> None:
        self._signaling_handler = handler

    def on_connection_change(self, handler: ConnectionHandler) -> None:
        self._connection_handler = handler

    def _emit_frames(self, chat_id: int, mode: Any, device: Any, frames: Sequence[Any]) -> None:
        if self._frames_received == 0 and frames:
            logger.info(
                "Call transport: first inbound frame (chat_id=%s, mode=%s)",
                chat_id,
                getattr(mode, "name", mode),
            )
        self._frames_received += len(frames)
        handler = self._frame_handler
        if handler is None:
            return
        mode_name = getattr(mode, "name", str(mode))
        device_name = getattr(device, "name", str(device))
        for frame in frames:
            data = getattr(frame, "data", None)
            if data:
                try:
                    handler(int(chat_id), str(mode_name), str(device_name), bytes(data))
                except Exception:  # noqa: BLE001 - native thread must not crash
                    logger.exception("Call frame handler failed")

    def _emit_signaling(self, chat_id: int, data: bytes) -> None:
        self._signaling_sent += 1
        if self._signaling_sent == 1:
            logger.info(
                "Call transport: first signaling packet for Telegram (%d bytes, chat_id=%s)",
                len(data),
                chat_id,
            )
        handler = self._signaling_handler
        if handler is None:
            logger.warning(
                "Call transport: signaling packet dropped; no handler is registered"
            )
            return
        try:
            handler(int(chat_id), bytes(data))
        except Exception:  # noqa: BLE001
            logger.exception("Call signaling handler failed")

    def _emit_connection(self, chat_id: int, info: Any) -> None:
        state = getattr(getattr(info, "state", info), "name", str(info))
        if state != self._last_state:
            self._last_state = str(state)
            logger.info("Call transport: state=%s (chat_id=%s)", state, chat_id)
        handler = self._connection_handler
        if handler is None:
            return
        try:
            handler(int(chat_id), str(state))
        except Exception:  # noqa: BLE001
            logger.exception("Call connection handler failed")

    def register_connection_chat_id(self, user_id: int, chat_id: int) -> None:
        """Register the ntgcalls chat_id for a user_id (peer_id)."""
        self._connection_chat_ids[user_id] = chat_id
        logger.debug("Registered ntgcalls chat_id=%s for user_id=%s", chat_id, user_id)

    def get_connection_chat_id(self, user_id: int) -> int | None:
        """Get the ntgcalls chat_id for a user_id, if known."""
        return self._connection_chat_ids.get(user_id)

    def clear_connection_chat_id(self, user_id: int) -> None:
        """Clear the stored chat_id for a user_id."""
        if user_id in self._connection_chat_ids:
            del self._connection_chat_ids[user_id]
            logger.debug("Cleared ntgcalls chat_id for user_id=%s", user_id)

    async def prepare(self, user_id: int) -> None:
        """Register the call and configure the outbound (capture) stream."""
        rtc = self._ensure()
        # A previous call for this user may still be registered after a failed
        # teardown, and ntgcalls refuses to initialise the same user twice
        # ("Connection cannot be initialized more than once").
        try:
            await self._await(rtc.stop(user_id))
        except Exception:  # noqa: BLE001 - nothing to clear is the normal case
            logger.debug("Telegram call: no stale native call for %s", user_id)
        await self._await(rtc.create_p2p_call(user_id))
        await self.set_capture(user_id)

    async def set_capture(self, user_id: int) -> None:
        rtc = self._ensure()
        media = self._media()
        await self._await(rtc.set_stream_sources(user_id, self._module.StreamMode.CAPTURE, media))

    async def start_playback(self, user_id: int) -> None:
        """Route the peer's audio to us as frames, then unmute and resume."""
        rtc = self._ensure()
        media = self._media()
        await self._await(rtc.set_stream_sources(user_id, self._module.StreamMode.PLAYBACK, media))
        await self._await(rtc.unmute(user_id))
        await self._await(rtc.resume(user_id))

    def _media(self) -> Any:
        audio = self._module.AudioDescription(
            media_source=self._module.MediaSource.EXTERNAL,
            sample_rate=CALL_SAMPLE_RATE,
            channel_count=1,
            input="",
            keep_open=True,
        )
        return self._module.MediaDescription(audio, None, None, None)

    async def init_exchange(
        self,
        user_id: int,
        g: int,
        p: bytes,
        random: bytes,
        ga_hash: bytes | None,
    ) -> bytes:
        rtc = self._ensure()
        dh = self._module.DhConfig(g=int(g), p=bytes(p), random=bytes(random))
        result = await self._await(rtc.init_exchange(user_id, dh, ga_hash))
        return bytes(result)

    async def exchange_keys(self, user_id: int, g_a_or_b: bytes, fingerprint: int) -> Any:
        rtc = self._ensure()
        return await self._await(rtc.exchange_keys(user_id, bytes(g_a_or_b), int(fingerprint)))

    async def connect(
        self,
        user_id: int,
        connections: Iterable[Any],
        *,
        p2p_allowed: bool,
        custom_parameters: str | None = None,
        versions: Sequence[str] | None = None,
    ) -> None:
        """Open the WebRTC transport for a 1:1 call.

        ``versions`` must be the versions the **peer** advertised in its
        ``PhoneCallProtocol``; ntgcalls picks the highest version from that
        list that it supports (8/9 = signaling V2, 12/13 = V3). Passing our
        own list makes it select 13.0.0 (V3) even when the peer only speaks
        V2, the handshake never starts and the transport reports TIMEOUT
        after 10 s. The local list is only a fallback when the peer sent none.
        """
        rtc = self._ensure()
        info = protocol_info(self._module)
        if info is None:
            raise MediaError("Could not read the call protocol.")
        servers = build_rtc_servers(connections, self._module)
        if not servers:
            raise MediaError("Telegram returned no call endpoints.")
        peer_versions = [str(item) for item in (versions or []) if str(item).strip()]
        chosen_versions = peer_versions or list(info.library_versions)
        logger.info(
            "Call transport: connect_p2p servers=%d p2p_allowed=%s versions=%s source=%s",
            len(servers),
            bool(p2p_allowed),
            chosen_versions,
            "peer" if peer_versions else "local-fallback",
        )
        for server in servers:
            logger.debug(
                "Call transport: endpoint id=%s ipv4=%s port=%s turn=%s stun=%s "
                "tcp=%s peer_tag_bytes=%d",
                getattr(server, "id", None),
                getattr(server, "ipv4", None),
                getattr(server, "port", None),
                getattr(server, "turn", None),
                getattr(server, "stun", None),
                getattr(server, "tcp", None),
                len(getattr(server, "peer_tag", b"") or b""),
            )
        await self._await(
            rtc.connect_p2p(
                user_id,
                servers,
                chosen_versions,
                bool(p2p_allowed),
                custom_parameters,
            )
        )

    async def send_frame(self, user_id: int, pcm: bytes) -> None:
        """Send a PCM frame using the correct ntgcalls chat_id for the connection."""
        rtc = self._ensure()
        if len(pcm) != BYTES_PER_FRAME:
            raise MediaError(
                f"Call frames must be exactly {BYTES_PER_FRAME} bytes, got {len(pcm)}."
            )
        # Use the registered chat_id if available, otherwise fall back to user_id
        chat_id = self._connection_chat_ids.get(user_id, user_id)
        # FrameData() without arguments is the only constructor form that is
        # always valid; keyword arguments raise "incompatible constructor".
        frame_data = self._module.FrameData()
        await self._await(
            rtc.send_external_frame(chat_id, self._module.StreamDevice.MICROPHONE, pcm, frame_data)
        )
        self._frames_sent += 1

    async def feed_signaling(self, user_id: int, data: bytes) -> None:
        rtc = self._ensure()
        chat_id = self._connection_chat_ids.get(user_id, user_id)
        await self._await(rtc.send_signaling_data(chat_id, bytes(data)))
        self._signaling_received += 1
        if self._signaling_received == 1:
            logger.info(
                "Call transport: first signaling packet from Telegram (%d bytes, chat_id=%s)",
                len(data),
                chat_id,
            )

    def stats(self) -> dict[str, Any]:
        """Counters used to tell a silent signaling path from a broken ICE path."""
        return {
            "frames_sent": self._frames_sent,
            "frames_received": self._frames_received,
            "signaling_sent": self._signaling_sent,
            "signaling_received": self._signaling_received,
            "last_connection_state": self._last_state,
        }

    async def mute(self, user_id: int) -> None:
        chat_id = self._connection_chat_ids.get(user_id, user_id)
        await self._await(self._ensure().mute(chat_id))

    async def unmute(self, user_id: int) -> None:
        chat_id = self._connection_chat_ids.get(user_id, user_id)
        await self._await(self._ensure().unmute(chat_id))

    async def stop(self, user_id: int) -> None:
        if self._ntg is None:
            return
        try:
            chat_id = self._connection_chat_ids.get(user_id, user_id)
            await self._await(self._ntg.stop(chat_id))
        except Exception:  # noqa: BLE001 - teardown must not raise
            logger.debug("Stopping the native call failed", exc_info=True)
        finally:
            self.clear_connection_chat_id(user_id)

    async def state(self, user_id: int) -> dict[str, Any] | None:
        if self._ntg is None:
            return None
        try:
            chat_id = self._connection_chat_ids.get(user_id, user_id)
            state = await self._await(self._ntg.get_state(chat_id))
        except Exception:  # noqa: BLE001
            return None
        return {
            "muted": bool(getattr(state, "muted", False)),
            "video_paused": bool(getattr(state, "video_paused", False)),
            "video_stopped": bool(getattr(state, "video_stopped", False)),
            "presentation_paused": bool(getattr(state, "presentation_paused", False)),
            "presentation_stopped": bool(getattr(state, "presentation_stopped", False)),
        }

    def close(self) -> None:
        """Drop the native instance; the process is shutting down anyway."""
        self._ntg = None
        self._module = None
        self._connection_chat_ids.clear()
        self._last_state = None

    @staticmethod
    async def _await(value: Any) -> Any:
        """Await ntgcalls actions, tolerating a plain return value."""
        if asyncio.isfuture(value) or asyncio.iscoroutine(value):
            return await value
        return value