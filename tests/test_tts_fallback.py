"""TTS fallback, Edge synthesis and audio-format contract tests (all mocked)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from config import Settings
from telegram.audio import (
    BYTES_PER_FRAME,
    TTS_SAMPLE_RATE,
    split_frames,
    upsample_24k_to_48k,
)
from voice.edge_speech import EdgeSpeechEngine
from voice.edge_speech import TTS_SAMPLE_RATE as EDGE_SAMPLE_RATE
from voice.gemini_speech import GeminiSpeechEngine
from voice.speech_engine import (
    ComposedSpeechEngine,
    SpeechEngine,
    VoiceError,
    VoiceNotConfigured,
    build_speech_engine,
)

KEY = "unit-test-tts-key"
PCM = b"\x10\x00" * 2400  # 0.1 s of 24 kHz mono int16


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "openai_api_key": KEY,
        "speech_provider": "gemini",
        "tts_fallback_provider": "edge",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class _StubEngine:
    """Speech engine double recording calls; can be told to fail."""

    def __init__(
        self,
        *,
        audio: bytes = b"audio",
        error: Exception | None = None,
        configured: bool = True,
    ) -> None:
        self.audio = audio
        self.error = error
        self.configured = configured
        self.synth_calls: list[tuple[str, str]] = []
        self.transcribe_calls: list[bytes] = []

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "utterance.webm",
        language: str | None = None,
    ) -> str:
        self.transcribe_calls.append(audio)
        return "transcript"

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        self.synth_calls.append((text, response_format))
        if self.error is not None:
            raise self.error
        if response_format == "pcm":
            return PCM
        return self.audio


def _composed(
    primary: _StubEngine, fallback: _StubEngine | None
) -> ComposedSpeechEngine:
    return ComposedSpeechEngine(
        _StubEngine(),
        primary,
        fallback,
        stt_name="gemini",
        tts_name="gemini",
        fallback_name="edge",
    )


# --------------------------------------------------------------------------- #
# Fallback behaviour
# --------------------------------------------------------------------------- #
def test_fallback_runs_when_primary_tts_fails(caplog: pytest.LogCaptureFixture) -> None:
    primary = _StubEngine(error=VoiceError("Speech request failed."))
    fallback = _StubEngine(audio=b"edge-audio")
    engine = _composed(primary, fallback)

    with caplog.at_level(logging.INFO, logger="voice.speech_engine"):
        data = asyncio.run(engine.synthesize("Salom", response_format="pcm"))

    assert data == PCM
    assert len(primary.synth_calls) == 1
    assert len(fallback.synth_calls) == 1
    assert any("fallback (edge)" in record.message for record in caplog.records)


def test_primary_success_skips_the_fallback() -> None:
    primary = _StubEngine(audio=b"primary-audio")
    fallback = _StubEngine(audio=b"edge-audio")
    engine = _composed(primary, fallback)

    assert asyncio.run(engine.synthesize("Salom")) == b"primary-audio"
    assert fallback.synth_calls == []


def test_unconfigured_primary_goes_straight_to_the_fallback() -> None:
    primary = _StubEngine(configured=False)
    fallback = _StubEngine(audio=b"edge-audio")
    engine = _composed(primary, fallback)

    assert asyncio.run(engine.synthesize("Salom")) == b"edge-audio"
    assert primary.synth_calls == []
    assert len(fallback.synth_calls) == 1


def test_both_providers_failing_raises_voice_error() -> None:
    primary = _StubEngine(error=VoiceError("primary failed"))
    fallback = _StubEngine(error=VoiceError("fallback failed"))
    engine = _composed(primary, fallback)

    with pytest.raises(VoiceError):
        asyncio.run(engine.synthesize("Salom"))
    assert len(primary.synth_calls) == 1
    assert len(fallback.synth_calls) == 1


def test_transcription_always_uses_the_speech_provider() -> None:
    stt = _StubEngine()
    primary = _StubEngine(error=VoiceError("429"))
    engine = ComposedSpeechEngine(
        stt, primary, _StubEngine(), stt_name="gemini", tts_name="gemini"
    )

    assert asyncio.run(engine.transcribe(b"audio")) == "transcript"
    assert stt.transcribe_calls == [b"audio"]
    assert engine.configured is True


# --------------------------------------------------------------------------- #
# Edge TTS engine
# --------------------------------------------------------------------------- #
class _FakeCommunicate:
    def __init__(self, module: "_FakeEdge", text: str, voice: str) -> None:
        self._module = module
        self.text = text
        self.voice = voice

    async def stream(self):
        self._module.calls.append(self)
        if self._module.fail:
            raise RuntimeError("edge endpoint unavailable")
        yield {"type": "audio", "data": b"ID3part1"}
        yield {"type": "WordBoundary", "offset": 1}
        yield {"type": "audio", "data": b"ID3part2"}


class _FakeEdge:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[_FakeCommunicate] = []

    def Communicate(self, text: str, voice: str) -> _FakeCommunicate:  # noqa: N802
        return _FakeCommunicate(self, text, voice)


def test_edge_engine_returns_24k_mono_pcm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeEdge()
    engine = EdgeSpeechEngine(_settings(edge_tts_voice="uz-UZ-SardorNeural"), engine=fake)
    converted: list[bytes] = []

    def fake_convert(audio: bytes) -> bytes:
        converted.append(audio)
        return PCM

    monkeypatch.setattr("voice.edge_speech._mp3_to_pcm", fake_convert)

    data = asyncio.run(engine.synthesize("Salom, Ksenus!", response_format="pcm"))

    assert data == PCM
    assert converted == [b"ID3part1ID3part2"]
    assert fake.calls[0].voice == "uz-UZ-SardorNeural"
    assert fake.calls[0].text == "Salom, Ksenus!"


def test_edge_engine_returns_mp3_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeEdge()
    engine = EdgeSpeechEngine(_settings(), engine=fake)

    def fail_convert(audio: bytes) -> bytes:  # pragma: no cover - must not run
        raise AssertionError("MP3 output must not be converted")

    monkeypatch.setattr("voice.edge_speech._mp3_to_pcm", fail_convert)

    assert asyncio.run(engine.synthesize("Salom")) == b"ID3part1ID3part2"


def test_edge_failure_becomes_voice_error_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = EdgeSpeechEngine(_settings(), engine=_FakeEdge(fail=True))

    with caplog.at_level(logging.WARNING, logger="voice.edge_speech"):
        with pytest.raises(VoiceError):
            asyncio.run(engine.synthesize("Salom"))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert KEY not in logged


def test_edge_without_package_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("voice.edge_speech._edge_tts", None)
    engine = EdgeSpeechEngine(_settings())

    assert engine.configured is False
    with pytest.raises(VoiceNotConfigured):
        asyncio.run(engine.synthesize("Salom"))


# --------------------------------------------------------------------------- #
# Configuration wiring
# --------------------------------------------------------------------------- #
def test_build_composes_gemini_with_edge_fallback() -> None:
    engine = build_speech_engine(_settings())

    assert isinstance(engine, ComposedSpeechEngine)
    assert engine.configured is True
    assert engine._fallback_name == "edge"
    assert engine._tts_name == "gemini"


def test_build_without_fallback_returns_the_plain_engine() -> None:
    engine = build_speech_engine(_settings(tts_fallback_provider=""))

    assert isinstance(engine, GeminiSpeechEngine)


def test_tts_provider_override_selects_edge_for_synthesis() -> None:
    settings = _settings(tts_provider="edge")

    assert settings.tts_backend == "edge"
    engine = build_speech_engine(settings)
    assert isinstance(engine, ComposedSpeechEngine)
    assert engine._tts_name == "edge"
    assert engine._stt_name == "gemini"


def test_openai_stack_keeps_openai_tts_when_fallback_disabled() -> None:
    settings = _settings(speech_provider="openai", tts_fallback_provider="")

    assert settings.tts_backend == "openai"
    assert isinstance(build_speech_engine(settings), SpeechEngine)


def test_tts_defaults_and_validation() -> None:
    settings = Settings(_env_file=None)  # type: ignore[arg-type]

    assert settings.tts_provider == ""
    assert settings.tts_backend == settings.speech_provider
    assert settings.tts_fallback_provider == "edge"
    assert settings.edge_tts_voice == "uz-UZ-MadinaNeural"

    with pytest.raises(ValidationError):
        Settings(_env_file=None, tts_fallback_provider="elevenlabs")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Audio format contract for the Telegram call path
# --------------------------------------------------------------------------- #
def test_pcm_matches_telegram_frame_contract() -> None:
    assert EDGE_SAMPLE_RATE == TTS_SAMPLE_RATE == 24_000
    assert len(PCM) % 2 == 0  # whole int16 samples only

    upsampled = upsample_24k_to_48k(PCM)
    assert len(upsampled) == 2 * len(PCM)  # exact 2x, still mono int16

    frames = split_frames(upsampled)
    assert len(frames) == 10  # 0.1 s of 10 ms frames
    assert all(len(frame) == BYTES_PER_FRAME == 960 for frame in frames)


def test_edge_engine_applies_the_configured_rate() -> None:
    import asyncio

    from config import Settings
    from voice.edge_speech import EdgeSpeechEngine

    seen: dict[str, object] = {}

    class _FakeCommunicate:
        def __init__(self, text, voice, *, rate=None, pitch=None, volume=None):
            seen.update(text=text, voice=voice, rate=rate, pitch=pitch, volume=volume)

        async def stream(self):
            yield {"type": "audio", "data": b"ID3fake"}

    class _FakeEdge:
        Communicate = _FakeCommunicate

    engine = EdgeSpeechEngine(
        Settings(_env_file=None, edge_tts_voice="uz-UZ-MadinaNeural"), engine=_FakeEdge
    )

    asyncio.run(engine.synthesize("Salom.", response_format="mp3"))

    assert seen["rate"] == "+25%"
    assert seen["voice"] == "uz-UZ-MadinaNeural"
