"""Call tuning: echo-safe barge-in, stale queue policy, STT model fallback."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from config import Settings
from fakes import FakeSpeech
from test_speech_limits import _wav_bytes
from test_voice_call import (
    FakeCallClient,
    FakeCallTransport,
    _call_settings,
    _loud_frame,
    _wait_for,
)
from test_voice_pipeline import _ScriptedSpeech, _utterance_pcm
from telegram.voice_call import VoiceCallSession
from voice.gemini_speech import GeminiSpeechEngine


class _LongSpeech(FakeSpeech):
    """Synthesises 2 s of audio so the session stays 'speaking' during a test."""

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        self.synthesized.append(text)
        self.formats.append(response_format)
        return b"\x10\x00" * (24_000 * 2)


def _session(settings: Settings, speech: FakeSpeech, handler) -> VoiceCallSession:
    return VoiceCallSession(
        settings,
        FakeCallTransport(),
        FakeCallClient(),
        peer_id=555,
        speech=speech,
        on_message=handler,
    )


# --------------------------------------------------------------------------- #
# Barge-in: echo must not interrupt, a real speaker must
# --------------------------------------------------------------------------- #
def test_short_echo_does_not_interrupt_but_sustained_speech_does(
    settings: Settings,
) -> None:
    asyncio.run(_echo_guard_scenario(_call_settings(settings)))


async def _echo_guard_scenario(settings: Settings) -> None:
    async def handler(text: str) -> str:
        return ""

    session = _session(settings, _LongSpeech(), handler)
    await session.place()
    session._listen_task = asyncio.create_task(session._listen_audio())

    await session._speak("Salom, men Ksenus. Sizni eshityapman.")
    assert session._speaking is True
    # Barge-in is ignored for the first 250 ms of playback (echo peak).
    await asyncio.sleep(0.3)

    # 50 ms of loud audio is speaker echo / a click, not an interruption.
    for _ in range(5):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
    await asyncio.sleep(0.1)
    assert session.status()["interruptions"] == 0

    # 300 ms of sustained speech above the raised threshold is a real barge-in.
    for _ in range(30):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
    assert await _wait_for(lambda: session.status()["interruptions"] == 1)
    await session.hangup()


# --------------------------------------------------------------------------- #
# Long speech: answered at the cap, never thrown away
# --------------------------------------------------------------------------- #
def test_endless_speech_is_answered_at_the_cap(settings: Settings) -> None:
    asyncio.run(_endless_speech(_call_settings(settings)))


async def _endless_speech(settings: Settings) -> None:
    """A microphone above the noise floor never ends an utterance by itself."""
    speech = _ScriptedSpeech(["uzun gap"])

    async def handler(text: str) -> str:
        return f"javob: {text}"

    session = _session(settings, speech, handler)
    await session.place()
    session._listen_task = asyncio.create_task(session._listen_audio())

    # 13 seconds of continuous speech, past the 12 s cap, no pause at all.
    for _ in range(1300):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
        await asyncio.sleep(0)

    assert await _wait_for(lambda: bool(speech.synthesized), attempts=120)
    assert speech.synthesized == ["javob: uzun gap"]
    assert session.status()["utterances_dropped"] == 0
    await session.hangup()


# --------------------------------------------------------------------------- #
# Queue: a stale utterance is skipped, not answered late
# --------------------------------------------------------------------------- #
def test_stale_utterance_is_skipped_instead_of_answered_late(
    settings: Settings,
) -> None:
    asyncio.run(_stale_queue_scenario(_call_settings(settings)))


async def _stale_queue_scenario(settings: Settings) -> None:
    tuned = settings.model_copy(update={"telegram_call_stale_utterance_seconds": 1.0})
    speech = _ScriptedSpeech(["birinchi gap", "ikkinchi gap"])
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        await asyncio.sleep(1.2)  # the first answer is slow
        return "javob"

    session = _session(tuned, speech, handler)
    await session.place()
    session._schedule_response(_utterance_pcm(1))
    session._schedule_response(_utterance_pcm(2))

    assert await _wait_for(lambda: session.status()["utterances_stale"] == 1, attempts=120)
    assert heard == ["birinchi gap"]
    assert session.status()["utterances_dropped"] == 0  # skipped, not lost to overflow
    assert session.status()["state"] != "ended"
    await session.hangup()


# --------------------------------------------------------------------------- #
# STT: rate-limited primary model falls back to the configured second model
# --------------------------------------------------------------------------- #
def test_stt_falls_back_to_the_second_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("voice.gemini_speech._sleep", fake_sleep)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "primary-stt" in request.url.path:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        body = json.loads(request.content)
        parts = body["contents"][0]["parts"]
        assert any("Transcribe the speech" in part.get("text", "") for part in parts)
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Salom"}]}}]}
        )

    settings = Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="unit-test-key",
        speech_provider="gemini",
        gemini_stt_model="primary-stt",
        gemini_stt_fallback_model="secondary-stt",
    )
    engine = GeminiSpeechEngine(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    assert asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 480))) == "Salom"
    assert any("secondary-stt" in path for path in seen)
