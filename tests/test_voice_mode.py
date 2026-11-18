"""Phone-call mode: call rules, token cap and the Edge-as-primary wiring."""

from __future__ import annotations

import asyncio

from brain import AgentBrain
from config import Settings
from fakes import FakeOpenAI, text_completion
from system_prompt import VOICE_RULES
from voice.edge_speech import EdgeSpeechEngine
from voice.speech_engine import ComposedSpeechEngine, build_speech_engine


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "openai_api_key": "unit-test-key",
        "llm_provider": "openai",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _system_message(client: FakeOpenAI) -> str:
    return str(client.requests[0]["messages"][0]["content"])


def test_voice_mode_adds_the_call_rules() -> None:
    client = FakeOpenAI([text_completion("Xo'p, eshitaman.")])
    brain = AgentBrain(_settings(), client=client)

    asyncio.run(brain.reply(history=[], user_message="Salom", voice_mode=True))

    system = _system_message(client)
    assert "Phone-call mode" in system
    assert VOICE_RULES in system


def test_chat_mode_keeps_the_previous_prompt() -> None:
    client = FakeOpenAI([text_completion("Salom!")])
    brain = AgentBrain(_settings(), client=client)

    asyncio.run(brain.reply(history=[], user_message="Salom"))

    assert "Phone-call mode" not in _system_message(client)


def test_max_tokens_is_sent_when_configured() -> None:
    client = FakeOpenAI([text_completion("Qisqa javob.")])
    brain = AgentBrain(_settings(llm_max_tokens=220), client=client)

    asyncio.run(brain.reply(history=[], user_message="Salom"))

    assert client.requests[0]["max_tokens"] == 220


def test_provider_default_is_kept_without_a_token_cap() -> None:
    client = FakeOpenAI([text_completion("Salom!")])
    brain = AgentBrain(_settings(), client=client)

    asyncio.run(brain.reply(history=[], user_message="Salom"))

    assert "max_tokens" not in client.requests[0]


def test_edge_primary_never_calls_gemini_tts() -> None:
    settings = Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="unit-test-key",
        speech_provider="gemini",
        tts_provider="edge",
        tts_fallback_provider="",
    )

    engine = build_speech_engine(settings)

    assert isinstance(engine, ComposedSpeechEngine)
    assert isinstance(engine._tts, EdgeSpeechEngine)
    assert engine._fallback is None  # no Gemini TTS request is made
    assert engine._stt_name == "gemini"
