"""Gemini speech engine tests. No network: the transport is mocked."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import wave
from collections.abc import Callable

import httpx
import pytest

from config import Settings
from voice.gemini_speech import GeminiSpeechEngine
from voice.speech_engine import VoiceError, VoiceNotConfigured

KEY = "unit-test-gemini-key"


def _wav_bytes(pcm: bytes, rate: int = 24_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "openai_api_key": KEY,
        "speech_provider": "gemini",
        "gemini_tts_model": "gemini-3.8-flash-tts",
        "gemini_stt_model": "gemini-3.5-transcribe",
        "gemini_voice": "Kore",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _engine(handler: Callable[[httpx.Request], httpx.Response]) -> GeminiSpeechEngine:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiSpeechEngine(_settings(), client=client)


def test_transcribe_reads_the_audio_transcription_field() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"audioTranscription": {"text": " Salom, Ksenus "}}]
                        }
                    }
                ]
            },
        )

    engine = _engine(handler)
    transcript = asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 4800)))

    assert transcript == "Salom, Ksenus"
    assert str(seen["path"]).endswith("gemini-3.5-transcribe:generateContent")
    assert seen["key"] == KEY
    payload = seen["payload"]
    assert isinstance(payload, dict)


def test_synthesize_pcm_returns_raw_24k_samples() -> None:
    pcm = b"\x10\x00" * 2400
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/wav",
                                        "data": base64.b64encode(_wav_bytes(pcm)).decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    engine = _engine(handler)
    audio = asyncio.run(engine.synthesize("Salom", response_format="pcm"))

    assert audio == pcm
    assert str(seen["path"]).endswith("gemini-3.8-flash-tts:generateContent")
    payload = seen["payload"]
    assert isinstance(payload, dict)
    config = payload["generationConfig"]
    assert config["responseModalities"] == ["AUDIO"]
    assert config["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"


@pytest.mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="ffmpeg is not installed"
)
def test_synthesize_mp3_encodes_the_pcm() -> None:
    pcm = b"\x10\x00" * 4800

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/wav",
                                        "data": base64.b64encode(_wav_bytes(pcm)).decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    engine = _engine(handler)
    audio = asyncio.run(engine.synthesize("Salom"))
    assert audio
    assert audio[:3] == b"ID3" or audio[0:1] == b"\xff"


def test_auth_failure_is_reported_without_leaking_the_key(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    engine = _engine(handler)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(VoiceError):
            asyncio.run(engine.transcribe(_wav_bytes(b"\x00\x00" * 480)))

    assert KEY not in caplog.text


def test_missing_key_reports_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = GeminiSpeechEngine(Settings(_env_file=None))
    assert engine.configured is False
    with pytest.raises(VoiceNotConfigured):
        asyncio.run(engine.synthesize("Salom"))


def test_a_rate_limited_stt_model_is_skipped_on_the_next_utterance() -> None:
    paths: list[str] = []
    payload = {
        "candidates": [
            {"content": {"parts": [{"audioTranscription": {"text": "salom"}}]}}
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "gemini-3.5-transcribe" in request.url.path:
            return httpx.Response(429, json={})
        return httpx.Response(200, json=payload)

    engine = GeminiSpeechEngine(
        _settings(gemini_stt_fallback_model="gemini-3.1-flash-lite"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    first = asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 4800)))
    before = len(paths)
    second = asyncio.run(engine.transcribe(_wav_bytes(b"\x02\x00" * 4800)))

    assert first == second == "salom"
    # The second utterance must not pay for the rate-limited model again.
    assert all("gemini-3.1-flash-lite" in path for path in paths[before:])
