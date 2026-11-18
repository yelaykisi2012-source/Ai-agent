"""Natural call behaviour: no canned text, token-cap retry, queue and TTS flow."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from brain import AgentBrain, BrainUnavailable
from config import Settings
from fakes import (
    FakeChoice,
    FakeCompletion,
    FakeMessage,
    FakeOpenAI,
    FakeSpeech,
    FakeTelegram,
    text_completion,
)
from main import create_app
from runtime import AgentRuntime
from test_speech_limits import _settings as _speech_settings
from test_speech_limits import _wav_bytes
from test_voice_call import (
    FakeCallClient,
    FakeCallTransport,
    _call_settings,
    _wait_for,
)
from test_voice_pipeline import _ScriptedSpeech, _utterance_pcm
from telegram.voice_call import VoiceCallSession
from voice.gemini_speech import GeminiSpeechEngine


def _empty_length() -> FakeCompletion:
    return FakeCompletion(
        choices=[FakeChoice(message=FakeMessage(content=""), finish_reason="length")]
    )


# --------------------------------------------------------------------------- #
# Brain: empty content and finish_reason=length
# --------------------------------------------------------------------------- #
def test_length_with_empty_text_retries_without_the_token_cap(
    settings: Settings,
) -> None:
    client = FakeOpenAI([_empty_length(), text_completion("Eshitaman.")])
    brain = AgentBrain(
        settings.model_copy(update={"llm_max_tokens": 220}), client=client
    )

    reply = asyncio.run(brain.reply(history=[], user_message="Salom"))

    assert reply.text == "Eshitaman."
    assert client.requests[0]["max_tokens"] == 220
    assert "max_tokens" not in client.requests[1]


def test_voice_turns_are_never_capped(settings: Settings) -> None:
    client = FakeOpenAI([text_completion("Ha, eshitaman.")])
    brain = AgentBrain(
        settings.model_copy(update={"llm_max_tokens": 220}), client=client
    )

    asyncio.run(brain.reply(history=[], user_message="Salom", voice_mode=True))

    assert "max_tokens" not in client.requests[0]


def test_still_empty_after_retry_raises_instead_of_inventing_text(
    settings: Settings,
) -> None:
    client = FakeOpenAI([_empty_length(), _empty_length()])
    brain = AgentBrain(
        settings.model_copy(update={"llm_max_tokens": 120}), client=client
    )

    with pytest.raises(BrainUnavailable):
        asyncio.run(brain.reply(history=[], user_message="Salom"))
    assert len(client.requests) == 2


def test_routed_model_is_logged(settings: Settings, caplog) -> None:
    served = text_completion("Xo'p.", model="some/other-model:free")
    client = FakeOpenAI([served])
    brain = AgentBrain(settings, client=client)

    with caplog.at_level(logging.INFO, logger="brain"):
        asyncio.run(brain.reply(history=[], user_message="Salom"))

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Chat model routed" in messages
    assert "some/other-model:free" in messages


def test_failed_call_turn_says_nothing_instead_of_a_canned_sentence(
    settings: Settings, caplog
) -> None:
    class _FailingBrain:
        def __init__(self) -> None:
            self.tools = None

        async def reply(self, **_kwargs):
            raise BrainUnavailable(
                "The model returned an empty reply. Try again or change the model."
            )

        def set_tool_registry(self, _registry) -> None:
            return None

    runtime = AgentRuntime(
        settings,
        brain=_FailingBrain(),
        telegram=FakeTelegram(),
        speech=FakeSpeech(),
    )
    app = create_app(settings, runtime=runtime)

    with TestClient(app):
        with caplog.at_level(logging.WARNING, logger="runtime"):
            answer = asyncio.run(runtime.handle_call_message("Salom"))

    assert answer is None  # silence, not the 65-character apology
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "turn skipped" in logged


# --------------------------------------------------------------------------- #
# STT: a configured fallback model is tried exactly once
# --------------------------------------------------------------------------- #
def test_stt_goes_straight_to_the_fallback_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("voice.gemini_speech._sleep", fake_sleep)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if "primary-stt" in request.url.path:
            return httpx.Response(
                429, headers={"retry-after": "30"}, json={"error": {"message": "quota"}}
            )
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Salom"}]}}]}
        )

    base = _speech_settings()
    settings = base.model_copy(
        update={
            "gemini_stt_model": "primary-stt",
            "gemini_stt_fallback_model": "secondary-stt",
        }
    )
    engine = GeminiSpeechEngine(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    assert asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 480))) == "Salom"
    assert len(requests) == 2  # one primary attempt, one fallback attempt
    assert any("secondary-stt" in path for path in requests)


def test_stt_retry_after_is_capped_for_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("voice.gemini_speech._sleep", fake_sleep)
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        if len(requests) == 1:
            return httpx.Response(
                429, headers={"retry-after": "30"}, json={"error": {"message": "q"}}
            )
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "Salom"}]}}]}
        )

    engine = GeminiSpeechEngine(
        _speech_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert asyncio.run(engine.transcribe(_wav_bytes(b"\x01\x00" * 480))) == "Salom"
    assert sum(delays) <= 2.0  # a 30 s Retry-After must not stall the call


# --------------------------------------------------------------------------- #
# Queue: newer speech wins over an outdated answer
# --------------------------------------------------------------------------- #
def test_older_utterance_is_superseded_by_a_newer_one(settings: Settings) -> None:
    asyncio.run(_superseded_scenario(_call_settings(settings)))


async def _superseded_scenario(settings: Settings) -> None:
    # Only two utterances reach STT: the superseded one never gets transcribed.
    speech = _ScriptedSpeech(["birinchi", "uchinchi"])
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        if len(heard) == 1:
            await asyncio.sleep(1.8)  # the first answer is very slow
        return f"javob: {text}"

    session = VoiceCallSession(
        settings,
        FakeCallTransport(),
        FakeCallClient(),
        peer_id=555,
        speech=speech,
        on_message=handler,
    )
    await session.place()
    for index in range(3):
        session._schedule_response(_utterance_pcm(index + 1))

    assert await _wait_for(
        lambda: session.status()["utterances_superseded"] == 1, attempts=120
    )
    assert heard == ["birinchi", "uchinchi"]
    assert speech.transcribed and len(speech.transcribed) == 2
    assert session.status()["state"] != "ended"
    await session.hangup()


# --------------------------------------------------------------------------- #
# TTS: sentence-level pipeline keeps the frame contract
# --------------------------------------------------------------------------- #
def test_reply_is_synthesised_sentence_by_sentence(settings: Settings) -> None:
    asyncio.run(_sentence_pipeline_scenario(_call_settings(settings)))


async def _sentence_pipeline_scenario(settings: Settings) -> None:
    speech = FakeSpeech()

    async def handler(text: str) -> str:
        return ""

    session = VoiceCallSession(
        settings,
        FakeCallTransport(),
        FakeCallClient(),
        peer_id=555,
        speech=speech,
        on_message=handler,
    )
    await session.place()

    frames = await session._speak("Birinchi gap. Ikkinchi gap. Uchinchi gap.")

    assert speech.synthesized == ["Birinchi gap.", "Ikkinchi gap.", "Uchinchi gap."]
    # Three 0.1 s replies (10 frames each) plus the two 60 ms sentence gaps
    # that replaced the providers' silence padding (12 frames).
    assert frames == 42
    assert session._outbox.qsize() == 42
    await session.hangup()


def test_call_vad_defaults_are_echo_safe() -> None:
    settings = Settings(_env_file=None)  # type: ignore[arg-type]

    assert settings.telegram_call_silence_ms == 900
    assert settings.telegram_call_barge_in_ms == 300
    assert settings.telegram_call_barge_in_threshold_scale == 2.0
    assert settings.telegram_call_barge_in_grace_ms == 250
    assert settings.telegram_call_stale_utterance_seconds == 6.0
    assert settings.gemini_stt_timeout_seconds == 8.0
    assert settings.gemini_stt_total_seconds == 12.0


# --------------------------------------------------------------------------- #
# Evidence-driven fixes from the real 14:40 call
# --------------------------------------------------------------------------- #
def test_edge_retries_once_when_the_stream_has_no_audio() -> None:
    from voice.edge_speech import EdgeSpeechEngine

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def Communicate(self, text, voice):  # noqa: N802
            self.calls += 1
            calls = self.calls

            class _Stream:
                async def stream(self_inner):
                    if calls == 1:  # first attempt: NoAudioReceived
                        raise RuntimeError("NoAudioReceived")
                    yield {"type": "audio", "data": b"ID3audio"}

            return _Stream()

    engine = EdgeSpeechEngine(
        _speech_settings().model_copy(update={"edge_tts_voice": "uz-UZ-MadinaNeural"}),
        engine=_Flaky(),
    )

    assert asyncio.run(engine.synthesize("Salom")) == b"ID3audio"


def test_voice_reply_is_trimmed_at_a_sentence_boundary() -> None:
    from runtime import _trim_voice_reply

    long_reply = "Birinchi gap shu yerda tugaydi. " * 12
    trimmed = _trim_voice_reply(long_reply, 120)

    assert len(trimmed) <= 120
    assert trimmed.endswith(".")
    assert "Birinchi gap shu yerda tugaydi." in trimmed
    # Short replies and a disabled limit are untouched.
    assert _trim_voice_reply("Qisqa javob.", 120) == "Qisqa javob."
    assert _trim_voice_reply(long_reply, 0) == long_reply
