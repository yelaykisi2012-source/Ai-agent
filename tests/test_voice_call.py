"""Live call session tests with a fake transport. No network, no real audio."""

from __future__ import annotations

import asyncio
from array import array
from types import SimpleNamespace
from typing import Any

from config import Settings
from database import init_database
from fakes import FakeSpeech
from telegram.audio import SILENCE_FRAME
from telegram.calls import CallManager
from telegram.voice_call import VoiceCallSession


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _loud_frame(amplitude: int = 6000) -> bytes:
    return array("h", [amplitude] * 480).tobytes()


def _connection() -> Any:
    return SimpleNamespace(
        id=11,
        ip="149.154.167.40",
        ipv6="",
        port=443,
        peer_tag=b"\x01" * 16,
        tcp=True,
    )


def _phone_call(
    *, call_id: int = 777, access_hash: int = 333, g_a_or_b: bytes | None = b"ga"
) -> Any:
    call = type("PhoneCall", (), {})()
    call.id = call_id
    call.access_hash = access_hash
    call.g_a_or_b = g_a_or_b
    call.key_fingerprint = 42
    call.connections = [_connection()]
    call.p2p_allowed = True
    call.custom_parameters = None
    return call


def _accepted(*, g_b: bytes = b"gb", call_id: int = 777, access_hash: int = 333) -> Any:
    call = type("PhoneCallAccepted", (), {})()
    call.id = call_id
    call.access_hash = access_hash
    call.g_b = g_b
    call.key_fingerprint = 42
    return call


def _requested(call_id: int = 888, access_hash: int = 444) -> Any:
    call = type("PhoneCallRequested", (), {})()
    call.id = call_id
    call.access_hash = access_hash
    call.g_a_hash = b"hash"
    call.admin_id = 555
    call.participant_id = 999
    return call


def _update(call: Any) -> Any:
    update = type("UpdatePhoneCall", (), {})()
    update.phone_call = call
    return update


class FakeCallClient:
    """Telethon-shaped client that records requests."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.connected_call = _phone_call()
        self.accepted_call = _phone_call(g_a_or_b=None)

    async def get_input_entity(self, peer: int) -> Any:
        return SimpleNamespace(user_id=peer, id=peer)

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        name = type(request).__name__
        if name == "GetDhConfigRequest":
            return SimpleNamespace(g=3, p=b"\x17" * 8, random=b"\x00" * 256)
        if name == "RequestCallRequest":
            return SimpleNamespace(phone_call=SimpleNamespace(id=777, access_hash=333))
        if name == "ConfirmCallRequest":
            return SimpleNamespace(phone_call=self.connected_call)
        if name == "AcceptCallRequest":
            return SimpleNamespace(phone_call=self.accepted_call)
        return True

    def of_type(self, name: str) -> list[Any]:
        return [item for item in self.requests if type(item).__name__ == name]


class FakeCallTransport:
    available = True
    version = "fake"

    def __init__(self) -> None:
        self.frame_handler: Any = None
        self.signaling_handler: Any = None
        self.connection_handler: Any = None
        self.prepared: list[int] = []
        self.exchanges: list[tuple[int, bytes | None]] = []
        self.exchange_keys_calls: list[tuple[int, bytes, int]] = []
        self.connects: list[tuple[int, list[Any], bool]] = []
        self.playback: list[int] = []
        self.stopped: list[int] = []
        self.frames: list[bytes] = []
        self.fed: list[bytes] = []
        self.connection_chat_ids: dict[int, int] = {}
        self.stats_calls = 0

    def on_frames(self, handler: Any) -> None:
        self.frame_handler = handler

    def on_signaling_data(self, handler: Any) -> None:
        self.signaling_handler = handler

    def on_connection_change(self, handler: Any) -> None:
        self.connection_handler = handler

    def devices(self) -> dict[str, list[str]]:
        return {"microphones": ["fake mic"], "speakers": ["fake speaker"]}

    async def prepare(self, user_id: int) -> None:
        self.prepared.append(user_id)

    async def init_exchange(
        self, user_id: int, g: int, p: bytes, random: bytes, ga_hash: bytes | None
    ) -> bytes:
        self.exchanges.append((user_id, ga_hash))
        return b"ga-hash" if ga_hash is None else b"gb"

    async def exchange_keys(self, user_id: int, g_a_or_b: bytes, fingerprint: int) -> Any:
        self.exchange_keys_calls.append((user_id, g_a_or_b, fingerprint))
        return SimpleNamespace(g_a_or_b=b"ga", key_fingerprint=fingerprint or 42)

    async def connect(
        self,
        user_id: int,
        connections: Any,
        *,
        p2p_allowed: bool,
        custom_parameters: str | None = None,
        versions: Any = None,
    ) -> None:
        self.connects.append((user_id, list(connections), p2p_allowed, versions))

    async def start_playback(self, user_id: int) -> None:
        self.playback.append(user_id)

    async def send_frame(self, user_id: int, pcm: bytes) -> None:
        self.frames.append(pcm)

    async def feed_signaling(self, user_id: int, data: bytes) -> None:
        self.fed.append(data)

    def register_connection_chat_id(self, user_id: int, chat_id: int) -> None:
        self.connection_chat_ids[user_id] = chat_id

    def get_connection_chat_id(self, user_id: int) -> int | None:
        return self.connection_chat_ids.get(user_id)

    def clear_connection_chat_id(self, user_id: int) -> None:
        self.connection_chat_ids.pop(user_id, None)

    async def stop(self, user_id: int) -> None:
        self.stopped.append(user_id)


class FakeAccount:
    def __init__(self, *, ready: bool = True, self_id: int = 999, owner_id: int = 999):
        self.ready = ready
        self.self_id = self_id
        self.owner_id = owner_id
        self.client = FakeCallClient()
        self.sent: list[str] = []

    async def send_message(self, text: str, user_id: int | None = None) -> bool:
        self.sent.append(text)
        return True


async def _echo_message(text: str) -> str:
    return f"Echo: {text}"


def _call_settings(settings: Settings) -> Settings:
    settings.telegram_calls_enabled = True
    settings.telegram_owner_user_id = 555
    settings.telegram_call_auto_answer = True
    return settings


async def _wait_for(predicate: Any, *, attempts: int = 60, delay: float = 0.05) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return False


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def test_outgoing_call_dials_and_streams_two_way_audio(settings: Settings) -> None:
    asyncio.run(_outgoing_scenario(_call_settings(settings)))


async def _outgoing_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    client = FakeCallClient()
    speech = FakeSpeech(transcript="Meni eshityapsanmi?")
    heard: list[str] = []
    finished: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return "Ha, eshitaman."

    session = VoiceCallSession(
        settings,
        transport,
        client,
        peer_id=555,
        speech=speech,
        on_message=handler,
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )

    started, detail = await session.place()
    assert started is True
    assert "calling" in detail.lower()
    assert transport.prepared == [555]
    request = client.of_type("RequestCallRequest")[0]
    assert request.g_a_hash == b"ga-hash"
    assert request.video is False

    await session.handle_phone_call(_accepted())
    confirm = client.of_type("ConfirmCallRequest")[0]
    assert confirm.g_a == b"ga"
    assert confirm.key_fingerprint == 42
    assert transport.connects and transport.connects[0][0] == 555
    assert transport.playback == [555]
    assert session.connected is True

    # Inbound audio: one second of speech, then enough silence to end it.
    # Real frames arrive in real time, so wait for the greeting to finish;
    # pushing them instantly would land entirely inside the barge-in grace.
    assert await _wait_for(lambda: speech.synthesized and not session._speaking, attempts=60)
    for _ in range(60):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
    for _ in range(110):  # silence_ms default is 900 ms
        session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)

    assert await _wait_for(lambda: bool(heard) and "pcm" in speech.formats)
    assert heard == ["Meni eshityapsanmi?"]
    assert speech.transcribed
    assert any(frame != SILENCE_FRAME for frame in transport.frames)

    await session.handle_signaling(b"candidate")
    assert transport.fed == [b"candidate"]

    ended, _ = await session.hangup()
    assert ended is True
    assert client.of_type("DiscardCallRequest")
    assert 555 in transport.stopped
    assert finished == ["owner_hangup"]
    assert session.status()["state"] == "ended"


def test_incoming_call_is_accepted_and_connected(settings: Settings) -> None:
    asyncio.run(_incoming_scenario(_call_settings(settings)))


async def _incoming_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    client = FakeCallClient()
    session = VoiceCallSession(
        settings,
        transport,
        client,
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )

    answered, detail = await session.accept(_requested())
    assert answered is True
    assert "answered" in detail.lower()
    accept = client.of_type("AcceptCallRequest")[0]
    assert accept.g_b == b"gb"
    assert accept.peer.id == 888

    await session.handle_phone_call(_phone_call(call_id=888, access_hash=444))
    assert session.connected is True
    assert transport.exchange_keys_calls == [(555, b"ga", 42)]
    await session.hangup()


def test_unanswered_call_times_out_with_a_missed_reason(settings: Settings) -> None:
    asyncio.run(_timeout_scenario(_call_settings(settings)))


async def _timeout_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    client = FakeCallClient()
    session = VoiceCallSession(
        settings,
        transport,
        client,
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.place()
    await session._timeout_after(0, "ring timeout")

    assert session.status()["state"] == "ended"
    discard = client.of_type("DiscardCallRequest")[0]
    assert type(discard.reason).__name__ == "PhoneCallDiscardReasonMissed"
    assert 555 in transport.stopped


def test_connection_loss_recovers_within_the_grace_period(settings: Settings) -> None:
    asyncio.run(_recovery_scenario(_call_settings(settings)))


async def _recovery_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.place()
    await session.handle_phone_call(_phone_call())

    session.connection_changed(555, "FAILED")
    await asyncio.sleep(0.01)
    session.connection_changed(555, "CONNECTED")
    await asyncio.sleep(0.05)
    assert session.active is True
    await session.hangup()


def test_connection_loss_without_recovery_ends_the_call(settings: Settings) -> None:
    settings.telegram_call_reconnect_grace_seconds = 0
    asyncio.run(_drop_scenario(_call_settings(settings)))


async def _drop_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    finished: list[str] = []
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )
    await session.place()
    await session.handle_phone_call(_phone_call())

    session.connection_changed(555, "TIMEOUT")
    assert await _wait_for(lambda: bool(finished))
    assert finished == ["connection_lost"]
    assert session.status()["state"] == "ended"


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
def test_manager_dials_only_the_configured_owner(settings: Settings) -> None:
    asyncio.run(_manager_dial_scenario(_call_settings(settings)))


async def _manager_dial_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    started, _detail = await manager.place("test call")
    assert started is True
    assert manager.active is True
    assert transport.prepared == [555]

    second, second_detail = await manager.place("second call")
    assert second is False
    assert "already in progress" in second_detail.lower()

    ended, _ = await manager.hangup()
    assert ended is True
    assert manager.active is False
    calls, total = manager.history()
    assert total >= 1
    assert any(call.state in {"ended", "active"} for call in calls)


def test_manager_refuses_without_owner_or_permission(settings: Settings) -> None:
    asyncio.run(_manager_refusal_scenario(settings))


async def _manager_refusal_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    manager = CallManager(settings, account, transport=FakeCallTransport())
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    disabled, detail = await manager.place("x")
    assert disabled is False
    assert "disabled" in detail.lower() or "cannot" in detail.lower()

    settings.telegram_calls_enabled = True
    settings.telegram_owner_user_id = None
    account.owner_id = account.self_id
    no_owner, owner_detail = await manager.place("x")
    assert no_owner is False
    assert "owner" in owner_detail.lower()


def test_guest_incoming_call_is_answered_when_public_calls_are_enabled(
    settings: Settings,
) -> None:
    asyncio.run(_guest_call_scenario(_call_settings(settings), enabled=True))


def test_guest_incoming_call_is_declined_when_public_calls_are_disabled(
    settings: Settings,
) -> None:
    asyncio.run(_guest_call_scenario(_call_settings(settings), enabled=False))


async def _guest_call_scenario(settings: Settings, *, enabled: bool) -> None:
    settings.telegram_public_calls_enabled = enabled
    # A guest cannot approve anything in the owner's UI, so their call must not
    # wait for the owner's auto-answer setting.
    settings.telegram_call_auto_answer = False
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    intruder = _requested()
    intruder.admin_id = 111
    await manager.handle_update(_update(intruder))

    if enabled:
        assert manager.active is True
        assert account.client.of_type("AcceptCallRequest")
        await manager.hangup()
        return

    assert manager.active is False
    assert account.sent  # the owner is told what happened
    calls, total = manager.history()
    assert total == 1
    assert calls[0].state == "declined"


def test_owner_incoming_call_is_answered_automatically(settings: Settings) -> None:
    asyncio.run(_auto_answer_scenario(_call_settings(settings)))


async def _auto_answer_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.handle_update(_update(_requested()))
    assert manager.active is True
    assert account.client.of_type("AcceptCallRequest")
    await manager.hangup()


def test_incoming_call_can_wait_for_owner_approval(settings: Settings) -> None:
    asyncio.run(_approval_scenario(_call_settings(settings)))


async def _approval_scenario(settings: Settings) -> None:
    settings.telegram_call_auto_answer = False
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.handle_update(_update(_requested()))
    assert manager.active is False
    assert manager.status()["pending_incoming"] is True

    answered, _detail = await manager.answer()
    assert answered is True
    assert manager.active is True
    await manager.hangup()


def test_manager_status_and_diagnostics_are_explicit(settings: Settings) -> None:
    account = FakeAccount()
    manager = CallManager(settings, account, transport=FakeCallTransport())
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    status = manager.status()
    assert status["active"] is False
    assert status["calls_enabled"] is False
    assert status["transport_available"] is True

    diagnostics = manager.transport_diagnostics()
    assert diagnostics["available"] is True
    assert diagnostics["media_enabled"] is False
    assert diagnostics["devices"]["microphones"] == ["fake mic"]


def test_hangup_without_a_call_is_reported(settings: Settings) -> None:
    manager = CallManager(settings, FakeAccount(), transport=FakeCallTransport())
    ended, detail = asyncio.run(manager.hangup())
    assert ended is False
    assert "no active" in detail.lower()


# --------------------------------------------------------------------------- #
# Connection lifecycle (regressions)
# --------------------------------------------------------------------------- #
class ConnectionNotFound(Exception):
    """Stands in for ``ntgcalls.ConnectionNotFound`` (matched by name)."""


class _DroppingTransport(FakeCallTransport):
    """Transport whose native call disappears right after the first frame."""

    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def send_frame(self, user_id: int, pcm: bytes) -> None:
        self.send_attempts += 1
        raise ConnectionNotFound("connection was removed")


def test_signaling_before_connect_is_replayed_in_order(settings: Settings) -> None:
    asyncio.run(_early_signaling_scenario(_call_settings(settings)))


async def _early_signaling_scenario(settings: Settings) -> None:
    """The peer's initial setup arrives before connect_p2p; it must survive.

    Dropping it leaves ntgcalls without remote ICE parameters: the transport
    stays Connecting, reports TIMEOUT after 10 s and the call dies.
    """

    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    answered, _detail = await session.accept(_requested())
    assert answered is True

    await session.handle_signaling(b"setup")
    await session.handle_signaling(b"candidate")
    # Queued, not fed to a native call that has no signaling channel yet.
    assert transport.fed == []

    await session.handle_phone_call(_phone_call(call_id=888, access_hash=444))
    assert session.connected is True
    assert transport.fed == [b"setup", b"candidate"]

    await session.handle_signaling(b"late-candidate")
    assert transport.fed == [b"setup", b"candidate", b"late-candidate"]
    await session.hangup()


def test_frames_are_paused_while_the_transport_reports_a_failure(
    settings: Settings,
) -> None:
    asyncio.run(_paused_transport_scenario(_call_settings(settings)))


async def _paused_transport_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.place()
    await session.handle_phone_call(_phone_call())
    assert await _wait_for(lambda: bool(transport.frames))

    session.connection_changed(555, "TIMEOUT")
    await asyncio.sleep(0.02)
    transport.frames.clear()

    # A dead connection must not be hammered with 100 frames per second.
    await asyncio.sleep(0.08)
    assert transport.frames == []

    # Signaling keeps flowing during the recovery window.
    await session.handle_signaling(b"ice")
    assert transport.fed == [b"ice"]

    session.connection_changed(555, "CONNECTED")
    await asyncio.sleep(0.02)
    assert session.active is True
    await session.hangup()


def test_vanished_native_call_ends_the_call_once_as_connection_lost(
    settings: Settings,
) -> None:
    asyncio.run(_dropped_connection_scenario(_call_settings(settings)))


async def _dropped_connection_scenario(settings: Settings) -> None:
    transport = _DroppingTransport()
    finished: list[str] = []
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )
    await session.place()
    await session.handle_phone_call(_phone_call())

    assert await _wait_for(lambda: bool(finished))
    assert finished == ["connection_lost"]
    assert session.status()["state"] == "ended"
    assert session.status()["error"] == "ConnectionNotFound"
    assert 555 in transport.stopped
    # One attempt, no per-frame retry storm against a stopped connection.
    assert transport.send_attempts == 1
    assert session._pump_task is None


def test_hangup_stops_and_awaits_the_audio_tasks(settings: Settings) -> None:
    asyncio.run(_task_teardown_scenario(_call_settings(settings)))


async def _task_teardown_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.place()
    await session.handle_phone_call(_phone_call())
    await session.hangup()

    current = asyncio.current_task()
    names = {
        task.get_name()
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    }
    assert "call-audio-out" not in names
    assert "call-audio-in" not in names
    assert "call-greeting" not in names
    assert session._pump_task is None
    assert session._listen_task is None


def test_signaling_update_is_routed_by_its_own_call_id(settings: Settings) -> None:
    asyncio.run(_signaling_routing_scenario(_call_settings(settings)))


def _signaling_update(call_id: int, data: bytes) -> Any:
    update = type("UpdatePhoneCallSignalingData", (), {})()
    update.phone_call_id = call_id
    update.data = data
    return update


async def _signaling_routing_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.place("test call")
    await manager.handle_update(_signaling_update(424242, b"not-ours"))
    assert transport.fed == []

    await manager.handle_update(_signaling_update(777, b"setup"))
    assert transport.fed == []

    await manager.handle_update(_update(_accepted()))
    assert transport.fed == [b"setup"]
    await manager.hangup()


# --------------------------------------------------------------------------- #
# Duplicate negotiation and foreign-call updates (audit regressions)
# --------------------------------------------------------------------------- #
class KeyAlreadyExchanged(Exception):
    """Stands in for ntgcalls' ``ConnectionError("Key already exchanged")``."""


class _StrictExchangeTransport(FakeCallTransport):
    """Transport that rejects a second DH exchange, like ntgcalls does."""

    def __init__(self) -> None:
        super().__init__()
        self.exchange_attempts = 0

    async def exchange_keys(
        self, user_id: int, g_a_or_b: bytes, fingerprint: int
    ) -> Any:
        self.exchange_attempts += 1
        attempt = self.exchange_attempts
        await asyncio.sleep(0)
        if attempt > 1:
            raise KeyAlreadyExchanged("Key already exchanged")
        return SimpleNamespace(g_a_or_b=b"ga", key_fingerprint=fingerprint or 42)


def test_duplicate_accepted_update_does_not_restart_negotiation(
    settings: Settings,
) -> None:
    asyncio.run(_duplicate_accepted_scenario(_call_settings(settings)))


async def _duplicate_accepted_scenario(settings: Settings) -> None:
    transport = _StrictExchangeTransport()
    finished: list[str] = []
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )
    started, _detail = await session.place()
    assert started is True
    await session.handle_phone_call(_accepted())

    # Telegram retransmits PhoneCallAccepted; a second exchange_keys would
    # make ntgcalls throw and the call would be marked failed.
    await session.handle_phone_call(_accepted())
    await session.handle_phone_call(_phone_call())
    assert finished == []
    assert transport.exchange_attempts == 1
    assert session.connected is True
    await session.hangup()


def test_concurrent_confirmed_updates_exchange_keys_once(settings: Settings) -> None:
    asyncio.run(_concurrent_confirmed_scenario(_call_settings(settings)))


async def _concurrent_confirmed_scenario(settings: Settings) -> None:
    transport = _StrictExchangeTransport()
    finished: list[str] = []
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )
    await session.accept(_requested())

    confirmed = _phone_call(call_id=888, access_hash=444)
    await asyncio.gather(
        session.handle_phone_call(confirmed),
        session.handle_phone_call(confirmed),
    )
    assert finished == []
    assert transport.exchange_attempts == 1
    assert session.connected is True
    await session.hangup()


def test_updates_for_another_call_do_not_touch_the_live_session(
    settings: Settings,
) -> None:
    asyncio.run(_foreign_update_scenario(_call_settings(settings)))


async def _foreign_update_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.place("test call")
    assert manager.active is True

    # A confirmed phone call that belongs to a different call id.
    await manager.handle_update(_update(_phone_call(call_id=999888, access_hash=1)))
    assert manager.active is True
    assert transport.connects == []

    discarded = type("PhoneCallDiscarded", (), {})()
    discarded.id = 999888
    discarded.access_hash = 1
    discarded.reason = None
    await manager.handle_update(_update(discarded))
    assert manager.active is True

    await manager.hangup()


def test_signaling_relay_from_a_native_thread_reaches_telegram(
    settings: Settings,
) -> None:
    asyncio.run(_threaded_relay_scenario(_call_settings(settings)))


async def _threaded_relay_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.place("test call")
    await manager.handle_update(_update(_accepted()))
    assert manager.active is True

    # ntgcalls invokes on_signaling_data on its own native thread.
    await asyncio.to_thread(transport.signaling_handler, 555, b"ice-from-native")
    assert await _wait_for(
        lambda: bool(account.client.of_type("SendSignalingDataRequest"))
    )
    request = account.client.of_type("SendSignalingDataRequest")[0]
    assert request.data == b"ice-from-native"
    assert request.peer.id == 777
    await manager.hangup()


# --------------------------------------------------------------------------- #
# Signaling protocol version selection (TIMEOUT root cause)
# --------------------------------------------------------------------------- #
def test_connect_uses_the_peer_protocol_versions(settings: Settings) -> None:
    asyncio.run(_peer_versions_scenario(_call_settings(settings)))


async def _peer_versions_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.accept(_requested())

    confirmed = _phone_call(call_id=888, access_hash=444)
    confirmed.protocol = SimpleNamespace(library_versions=["9.0.0"])
    await session.handle_phone_call(confirmed)

    assert transport.connects[-1][3] == ["9.0.0"]
    await session.hangup()


def test_connect_without_a_peer_protocol_passes_no_versions(settings: Settings) -> None:
    asyncio.run(_no_peer_versions_scenario(_call_settings(settings)))


async def _no_peer_versions_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.accept(_requested())
    await session.handle_phone_call(_phone_call(call_id=888, access_hash=444))
    assert transport.connects[-1][3] is None
    await session.hangup()


def test_status_reports_signaling_and_frame_counters(settings: Settings) -> None:
    asyncio.run(_counter_scenario(_call_settings(settings)))


async def _counter_scenario(settings: Settings) -> None:
    transport = FakeCallTransport()
    session = VoiceCallSession(
        settings,
        transport,
        FakeCallClient(),
        peer_id=555,
        speech=FakeSpeech(),
        on_message=_echo_message,
    )
    await session.accept(_requested())
    await session.handle_signaling(b"setup")
    await session.handle_phone_call(_phone_call(call_id=888, access_hash=444))
    session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)

    status = session.status()
    assert status["signaling_received"] == 1
    assert status["signaling_delivered"] == 1
    assert status["frames_received"] == 1
    assert await _wait_for(lambda: session.status()["frames_sent"] > 0)
    await session.hangup()


# --------------------------------------------------------------------------- #
# Incoming-call edge cases: owner approval, guests, cancelled calls
# --------------------------------------------------------------------------- #
def test_guest_call_is_declined_while_the_owner_call_waits(settings: Settings) -> None:
    asyncio.run(_guest_while_pending_scenario(_call_settings(settings)))


async def _guest_while_pending_scenario(settings: Settings) -> None:
    settings.telegram_call_auto_answer = False
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.handle_update(_update(_requested()))
    assert manager.status()["pending_incoming"] is True

    guest_call = _requested(call_id=999, access_hash=555)
    guest_call.admin_id = 111
    await manager.handle_update(_update(guest_call))

    # The owner keeps the line: the guest is refused and nothing is answered.
    assert manager.active is False
    assert manager.status()["pending_incoming"] is True
    calls, total = manager.history()
    assert total == 1
    assert calls[0].peer_id == 111
    assert calls[0].state == "declined"

    # The owner can still be answered afterwards.
    answered, _detail = await manager.answer()
    assert answered is True
    assert manager.active is True
    await manager.hangup()


def test_a_cancelled_waiting_call_frees_the_line(settings: Settings) -> None:
    asyncio.run(_cancelled_pending_scenario(_call_settings(settings)))


async def _cancelled_pending_scenario(settings: Settings) -> None:
    settings.telegram_call_auto_answer = False
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    pending = _requested()
    await manager.handle_update(_update(pending))
    assert manager.status()["pending_incoming"] is True

    discarded = type("PhoneCallDiscarded", (), {})()
    discarded.id = pending.id
    discarded.access_hash = pending.access_hash
    await manager.handle_update(_update(discarded))

    assert manager.status()["pending_incoming"] is False

    guest_call = _requested(call_id=1001, access_hash=557)
    guest_call.admin_id = 111
    await manager.handle_update(_update(guest_call))

    assert manager.active is True
    await manager.hangup()


def test_answering_is_refused_while_another_call_is_active(settings: Settings) -> None:
    asyncio.run(_answer_while_active_scenario(_call_settings(settings)))


async def _answer_while_active_scenario(settings: Settings) -> None:
    init_database(settings)
    account = FakeAccount()
    transport = FakeCallTransport()
    manager = CallManager(settings, account, transport=transport)
    manager.bind_pipeline(FakeSpeech(), _echo_message)

    await manager.handle_update(_update(_requested()))
    assert manager.active is True

    # Simulate a second waiting call arriving; it must never replace the live one.
    manager._pending_incoming = _requested(call_id=4321, access_hash=99)
    manager._pending_caller = settings.telegram_owner_user_id

    answered, detail = await manager.answer()

    assert answered is False
    assert "already in progress" in detail
    assert manager.active is True
    await manager.hangup()


# --------------------------------------------------------------------------- #
# Callbacks: "call me back in N minutes"
# --------------------------------------------------------------------------- #
def test_a_callback_request_schedules_the_call_and_ends_this_one(
    settings: Settings,
) -> None:
    asyncio.run(_callback_scenario(_call_settings(settings)))


async def _callback_scenario(settings: Settings) -> None:
    scheduled: list[int] = []
    finished: list[str] = []

    async def on_schedule(minutes: int) -> str:
        scheduled.append(minutes)
        return "18:45"

    speech = FakeSpeech(transcript="Xayr, 3 daqiqadan keyin qongiroq qil.")
    transport = FakeCallTransport()
    client = FakeCallClient()
    session = VoiceCallSession(
        settings,
        transport,
        client,
        peer_id=555,
        speech=speech,
        on_message=_echo_message,
        on_finished=lambda _s, reason, _d: finished.append(reason),
        on_schedule_call=on_schedule,
    )
    await session.place()
    await session.handle_phone_call(_accepted())
    assert await _wait_for(lambda: speech.synthesized and not session._speaking)

    for _ in range(60):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
        await asyncio.sleep(0)
    for _ in range(110):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)
        await asyncio.sleep(0)

    assert await _wait_for(lambda: scheduled == [3]), scheduled
    assert await _wait_for(lambda: "callback_scheduled" in finished), finished
    assert any("3 daqiqadan keyin" in text for text in speech.synthesized)


def test_a_plain_call_request_does_not_end_the_call(settings: Settings) -> None:
    asyncio.run(_no_callback_scenario(_call_settings(settings)))


async def _no_callback_scenario(settings: Settings) -> None:
    scheduled: list[int] = []

    async def on_schedule(minutes: int) -> str:
        scheduled.append(minutes)
        return "18:45"

    speech = FakeSpeech(transcript="Meni yaxshi eshityapsanmi?")
    session = VoiceCallSession(
        settings,
        FakeCallTransport(),
        FakeCallClient(),
        peer_id=555,
        speech=speech,
        on_message=_echo_message,
        on_schedule_call=on_schedule,
    )
    await session.place()
    await session.handle_phone_call(_accepted())
    assert await _wait_for(lambda: speech.synthesized and not session._speaking)

    for _ in range(60):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
        await asyncio.sleep(0)
    for _ in range(110):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)
        await asyncio.sleep(0)

    assert await _wait_for(lambda: len(speech.synthesized) >= 2)
    assert scheduled == []
    assert session.active is True
    await session.hangup()


def test_a_call_reminder_dials_instead_of_notifying(settings: Settings) -> None:
    from types import SimpleNamespace

    from runtime import AgentRuntime

    runtime = AgentRuntime(settings)
    placed: list[str] = []
    acknowledged: list[int] = []

    class _Calls:
        async def place(self, reason: str) -> tuple[bool, str]:
            placed.append(reason)
            return True, "dialing"

    class _Reminders:
        def acknowledge(self, reminder_id: int) -> bool:
            acknowledged.append(reminder_id)
            return True

    runtime.calls = _Calls()
    runtime.reminders = _Reminders()

    asyncio.run(runtime._run_scheduled_call(SimpleNamespace(id=42, kind="call")))

    assert placed == ["scheduled callback"]
    assert acknowledged == [42]
