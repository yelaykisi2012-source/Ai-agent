"""Gemini speech 429 handling: Retry-After, backoff, bounded retries, cache."""

from __future__ import annotations

import asyncio
import base64
import io
import wave
from collections.abc import Callable

import httpx
import pytest

from config import Settings
from voice.gemini_speech import GeminiSpeechEngine
from voice.speech_engine import VoiceError

KEY = "unit-test-gemini-key"


def _wav_bytes(pcm: bytes, rate: int = 24_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _settings() -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key=KEY,
        speech_provider="gemini",
        gemini_tts_model="gemini-3.8-flash-tts",
        gemini_stt_model="gemini-3.5-transcribe",
        gemini_voice="Kore",
    )


def _engine(handler: Callable[[httpx.Request], httpx.Response]) -> GeminiSpeechEngine:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiSpeechEngine(_settings(), client=client)


def _transcribe_body(text: str) -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"audioTranscription": {"text": text}}]}}
        ]
    }


def _tts_body(pcm: bytes) -> dict:
    audio = base64.b64encode(_wav_bytes(pcm)).decode()
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/wav", "data": audio}}
                    ]
                }
            }
        ]
    }


def _capture_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("voice.gemini_speech._sleep", fake_sleep)
    return delays


def test_stt_429_honours_retry_after_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _capture_sleeps(monkeypatch)
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "2"},
                json={"error": {"message": "quota exceeded"}},
            )
        return httpx.Response(200, json=_transcribe_body("Salom"))

    engine = _engine(handler)
    audio = _wav_bytes(b"\x01\x00" * 480)

    assert asyncio.run(engine.transcribe(audio)) == "Salom"
    assert len(requests) == 2
    # The 2 s Retry-After is honoured up to the 1.5 s STT cap; the wait is
    # split between our own sleep and the shared cooldown.
    assert delays and sum(delays) >= 1.4


def test_tts_429_is_retried_but_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_sleeps(monkeypatch)
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

    engine = _engine(handler)

    with pytest.raises(VoiceError):
        asyncio.run(engine.synthesize("Salom", response_format="pcm"))
    assert len(requests) == 3  # bounded: never an endless retry loop


def test_backoff_grows_without_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _capture_sleeps(monkeypatch)
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        if len(requests) == 1:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, json=_transcribe_body("Salom"))

    engine = _engine(handler)

    assert asyncio.run(engine.transcribe(_wav_bytes(bytes(960)))) == "Salom"
    assert len(delays) == 1
    # Each attempt waits longer than the previous one (jitter is +/-25%).
    assert GeminiSpeechEngine._backoff_delay(1) < GeminiSpeechEngine._backoff_delay(2)
    assert GeminiSpeechEngine._backoff_delay(2) < GeminiSpeechEngine._backoff_delay(3)


def test_tts_429_does_not_block_stt(monkeypatch: pytest.MonkeyPatch) -> None:
    """STT and TTS quotas are separate: one stage must not stall the other."""
    delays = _capture_sleeps(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stage = "tts" if "tts" in request.url.path else "stt"
        calls.append(stage)
        if stage == "tts":
            return httpx.Response(429, json={"error": {"message": "tts quota"}})
        return httpx.Response(200, json=_transcribe_body("Salom"))

    engine = _engine(handler)

    with pytest.raises(VoiceError):
        asyncio.run(engine.synthesize("Salom", response_format="pcm"))
    delays_after_tts = len(delays)

    assert asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 480))) == "Salom"
    assert len(delays) == delays_after_tts  # STT waited for no TTS cooldown
    assert calls.count("stt") == 1


def test_non_retryable_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays = _capture_sleeps(monkeypatch)
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    engine = _engine(handler)

    with pytest.raises(VoiceError):
        asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 480)))
    assert len(requests) == 1
    assert delays == []


def test_identical_audio_and_text_are_not_sent_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_sleeps(monkeypatch)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if "tts" in request.url.path:
            return httpx.Response(200, json=_tts_body(b"\x10\x00" * 240))
        return httpx.Response(200, json=_transcribe_body("Salom"))

    engine = _engine(handler)
    audio = _wav_bytes(b"\x01\x00" * 480)

    assert asyncio.run(engine.transcribe(audio)) == "Salom"
    assert asyncio.run(engine.transcribe(audio)) == "Salom"
    asyncio.run(engine.synthesize("Salom", response_format="pcm"))
    asyncio.run(engine.synthesize("Salom", response_format="pcm"))

    assert len(requests) == 2  # one STT and one TTS request in total
