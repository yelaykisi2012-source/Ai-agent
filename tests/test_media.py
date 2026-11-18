"""ntgcalls 3.0.7 <-> Telethon mapping tests. No network, no audio."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from telethon.tl import types

from config import Settings
from telegram.media import (
    NtgTransport,
    build_phone_call_protocol,
    build_rtc_servers,
    protocol_info,
)


def _relay_connection() -> types.PhoneConnection:
    return types.PhoneConnection(
        id=11,
        ip="149.154.167.40",
        ipv6="2001:67c:4e8:f002::a",
        port=443,
        peer_tag=b"\x01" * 16,
        tcp=True,
    )


def _webrtc_connection() -> types.PhoneConnectionWebrtc:
    return types.PhoneConnectionWebrtc(
        id=22,
        ip="149.154.167.50",
        ipv6="",
        port=443,
        username="user",
        password="secret",
        turn=True,
        stun=False,
    )


def test_relay_connection_becomes_a_turn_server_with_peer_tag() -> None:
    servers = build_rtc_servers([_relay_connection()])
    assert len(servers) == 1
    server = servers[0]
    assert server.id == 11
    assert server.ipv4 == "149.154.167.40"
    assert server.ipv6 == "2001:67c:4e8:f002::a"
    assert server.port == 443
    assert server.turn is True
    assert server.tcp is True
    assert server.peer_tag == b"\x01" * 16


def test_webrtc_connection_keeps_turn_credentials() -> None:
    servers = build_rtc_servers([_webrtc_connection()])
    assert len(servers) == 1
    server = servers[0]
    assert server.id == 22
    assert server.username == "user"
    assert server.password == "secret"
    assert server.turn is True
    assert server.stun is False


def test_both_connection_kinds_can_be_mixed() -> None:
    servers = build_rtc_servers([_relay_connection(), _webrtc_connection()])
    assert [server.id for server in servers] == [11, 22]


def test_protocol_layer_matches_the_installed_library() -> None:
    info = protocol_info()
    assert info is not None
    assert info.min_layer == 92
    assert info.max_layer == 92
    assert info.udp_p2p is True
    assert info.udp_reflector is True
    assert "8.0.0" in info.library_versions


def test_phone_call_protocol_is_valid_for_telethon() -> None:
    protocol = build_phone_call_protocol()
    assert isinstance(protocol, types.PhoneCallProtocol)
    assert protocol.min_layer == 92
    assert protocol.max_layer == 92
    assert protocol.udp_p2p is True
    assert protocol.udp_reflector is True
    assert protocol.library_versions


def test_transport_reports_the_native_library(settings: Settings) -> None:
    transport = NtgTransport(settings)
    assert transport.available is True
    assert transport.version == "3.0.7"
    devices = transport.devices()
    assert set(devices) == {"microphones", "speakers"}
    assert isinstance(devices["microphones"], list)


class _FakeModule:
    """Minimal ntgcalls stand-in for transport wiring tests."""

    class StreamMode:
        CAPTURE = "capture"
        PLAYBACK = "playback"

    class MediaSource:
        EXTERNAL = "external"

    class StreamDevice:
        MICROPHONE = "microphone"

    class NTgCalls:
        @staticmethod
        def get_protocol() -> Any:
            return SimpleNamespace(
                library_versions=["8.0.0", "9.0.0"],
                min_layer=92,
                max_layer=92,
                udp_p2p=True,
                udp_reflector=True,
            )

    class RTCServer:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    @staticmethod
    def AudioDescription(**kwargs: Any) -> Any:
        return ("audio", kwargs)

    @staticmethod
    def MediaDescription(*args: Any) -> Any:
        return ("media", args)

    @staticmethod
    def FrameData() -> Any:
        return ("frame-data",)


class _RecordingRtc:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.frames: list[tuple[int, str, bytes, Any]] = []

    async def stop(self, user_id: int) -> None:
        self.events.append(("stop", user_id))

    async def create_p2p_call(self, user_id: int) -> None:
        self.events.append(("create", user_id))

    async def set_stream_sources(self, user_id: int, mode: Any, media: Any) -> None:
        self.events.append(("sources", user_id, mode))

    async def send_external_frame(
        self, user_id: int, device: Any, pcm: bytes, frame_data: Any
    ) -> None:
        self.frames.append((user_id, device, pcm, frame_data))

    async def send_signaling_data(self, chat_id: int, data: bytes) -> None:
        self.events.append(("signal", chat_id, data))

    async def connect_p2p(
        self,
        user_id: int,
        servers: Any,
        versions: Any,
        p2p_allowed: bool,
        custom_parameters: Any,
    ) -> None:
        self.events.append(
            ("connect", user_id, list(versions), p2p_allowed, len(list(servers)))
        )


def _fake_transport() -> tuple[NtgTransport, _RecordingRtc]:
    transport = NtgTransport.__new__(NtgTransport)
    transport._settings = None  # type: ignore[attr-defined]
    transport._module = _FakeModule()  # type: ignore[attr-defined]
    transport._frame_handler = None  # type: ignore[attr-defined]
    transport._signaling_handler = None  # type: ignore[attr-defined]
    transport._connection_handler = None  # type: ignore[attr-defined]
    transport._connection_chat_ids = {}  # type: ignore[attr-defined]
    transport._frames_sent = 0  # type: ignore[attr-defined]
    transport._frames_received = 0  # type: ignore[attr-defined]
    transport._signaling_sent = 0  # type: ignore[attr-defined]
    transport._signaling_received = 0  # type: ignore[attr-defined]
    transport._last_state = None  # type: ignore[attr-defined]
    rtc = _RecordingRtc()
    transport._ntg = rtc  # type: ignore[attr-defined]
    return transport, rtc


def test_prepare_clears_a_stale_call_before_creating_a_new_one() -> None:
    transport, rtc = _fake_transport()
    asyncio.run(transport.prepare(7))
    assert [event[0] for event in rtc.events] == ["stop", "create", "sources"]
    assert rtc.events[0][1] == 7


def test_audio_frames_use_the_no_argument_frame_data_constructor() -> None:
    transport, rtc = _fake_transport()
    pcm = b"\x01\x00" * 480
    asyncio.run(transport.send_frame(7, pcm))
    assert rtc.frames == [(7, "microphone", pcm, ("frame-data",))]


def test_connect_forwards_the_peer_protocol_versions() -> None:
    """ntgcalls picks its signaling version from this list, so it must be the
    peer's advertised versions, never our own."""
    transport, rtc = _fake_transport()
    asyncio.run(
        transport.connect(7, [_relay_connection()], p2p_allowed=True, versions=["9.0.0"])
    )
    assert rtc.events[-1] == ("connect", 7, ["9.0.0"], True, 1)


def test_connect_falls_back_to_local_versions_without_a_peer_protocol() -> None:
    transport, rtc = _fake_transport()
    asyncio.run(transport.connect(7, [_webrtc_connection()], p2p_allowed=False))
    assert rtc.events[-1] == ("connect", 7, ["8.0.0", "9.0.0"], False, 1)


def test_transport_reports_diagnostic_counters() -> None:
    transport, _rtc = _fake_transport()
    asyncio.run(transport.feed_signaling(7, b"setup"))
    asyncio.run(transport.send_frame(7, bytes(960)))
    stats = transport.stats()
    assert stats["signaling_received"] == 1
    assert stats["frames_sent"] == 1
    assert stats["frames_received"] == 0
