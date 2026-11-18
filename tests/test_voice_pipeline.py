"""Conversation pipeline tests: stage order, bounded queue, provider failures."""

from __future__ import annotations

import asyncio
from array import array
from typing import Any

from config import Settings
from fakes import FakeSpeech
from test_voice_call import (
    FakeCallClient,
    FakeCallTransport,
    _call_settings,
    _wait_for,
)
from telegram.voice_call import VoiceCallSession
from voice.speech_engine import VoiceError


class _ScriptedSpeech(FakeSpeech):
    """One transcript per utterance; can fail selected TTS calls."""

    def __init__(
        self, transcripts: list[str], *, fail_tts_calls: set[int] | None = None
    ) -> None:
        super().__init__()
        self._transcripts = list(transcripts)
        self._fail_tts_calls = fail_tts_calls or set()
        self.tts_calls = 0

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "utterance.webm",
        language: str | None = None,
    ) -> str:
        self.transcribed.append(audio)
        return self._transcripts.pop(0) if self._transcripts else ""

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        self.tts_calls += 1
        if self.tts_calls in self._fail_tts_calls:
            raise VoiceError("Speech synthesis limit reached.")
        return await super().synthesize(text, response_format=response_format)


def _utterance_pcm(seed: int) -> bytes:
    return array("h", [seed] * 4800).tobytes()


def _session(settings: Settings, speech: FakeSpeech, handler: Any) -> VoiceCallSession:
    return VoiceCallSession(
        settings,
        FakeCallTransport(),
        FakeCallClient(),
        peer_id=555,
        speech=speech,
        on_message=handler,
    )


def test_answers_several_utterances_in_order(settings: Settings) -> None:
    asyncio.run(_ordered_utterances(_call_settings(settings)))


async def _ordered_utterances(settings: Settings) -> None:
    speech = _ScriptedSpeech(["birinchi gap", "ikkinchi gap", "uchinchi gap"])
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return f"javob: {text}"

    session = _session(settings, speech, handler)
    await session.place()
    for index in range(3):
        session._schedule_response(_utterance_pcm(index + 1))

    assert await _wait_for(lambda: len(heard) == 3, attempts=100)
    assert heard == ["birinchi gap", "ikkinchi gap", "uchinchi gap"]
    # TTS runs after every LLM reply: no reply is lost to the next utterance.
    assert speech.synthesized == [f"javob: {text}" for text in heard]
    assert session.status()["replies"] == 3
    assert session.status()["utterances"] == 3
    assert session.status()["state"] != "ended"
    await session.hangup()


def test_empty_llm_reply_does_not_block_the_queue(settings: Settings) -> None:
    asyncio.run(_empty_llm_reply(_call_settings(settings)))


async def _empty_llm_reply(settings: Settings) -> None:
    speech = _ScriptedSpeech(["birinchi gap", "ikkinchi gap"])
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        # OpenRouter can answer HTTP 200 with an empty assistant message.
        return "" if len(heard) == 1 else "javob bor"

    session = _session(settings, speech, handler)
    await session.place()
    session._schedule_response(_utterance_pcm(1))
    session._schedule_response(_utterance_pcm(2))

    assert await _wait_for(lambda: speech.synthesized == ["javob bor"], attempts=100)
    assert heard == ["birinchi gap", "ikkinchi gap"]
    assert session.status()["replies"] == 1
    assert session.status()["state"] != "ended"
    await session.hangup()


def test_tts_failure_does_not_block_the_next_utterance(settings: Settings) -> None:
    asyncio.run(_tts_failure(_call_settings(settings)))


async def _tts_failure(settings: Settings) -> None:
    speech = _ScriptedSpeech(["birinchi gap", "ikkinchi gap"], fail_tts_calls={1})
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return f"javob: {text}"

    session = _session(settings, speech, handler)
    await session.place()
    session._schedule_response(_utterance_pcm(1))
    session._schedule_response(_utterance_pcm(2))

    assert await _wait_for(lambda: len(speech.synthesized) == 1, attempts=100)
    assert speech.tts_calls == 2
    assert heard == ["birinchi gap", "ikkinchi gap"]
    assert speech.synthesized == ["javob: ikkinchi gap"]
    assert session.status()["state"] != "ended"
    await session.hangup()


def test_utterance_backlog_is_bounded(settings: Settings) -> None:
    asyncio.run(_bounded_backlog(_call_settings(settings)))


async def _bounded_backlog(settings: Settings) -> None:
    speech = _ScriptedSpeech([f"gap {n}" for n in range(1, 7)])
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        await asyncio.sleep(0.01)
        return f"javob: {text}"

    session = _session(settings, speech, handler)
    await session.place()
    for index in range(6):
        session._schedule_response(_utterance_pcm(index + 1))

    # The backlog holds 3 utterances; the rest are dropped, never queued forever.
    assert session.status()["utterances_dropped"] == 3
    assert await _wait_for(lambda: len(heard) == 3, attempts=100)
    assert session.status()["state"] != "ended"
    await session.hangup()


def test_duplicate_transcript_is_answered_once(settings: Settings) -> None:
    asyncio.run(_duplicate_transcript(_call_settings(settings)))


async def _duplicate_transcript(settings: Settings) -> None:
    # VAD can split one sentence into two identical transcripts.
    speech = _ScriptedSpeech(["bir xil gap", "bir xil gap"])
    heard: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return "javob"

    session = _session(settings, speech, handler)
    await session.place()
    session._schedule_response(_utterance_pcm(1))
    session._schedule_response(_utterance_pcm(2))

    assert await _wait_for(lambda: bool(speech.synthesized), attempts=100)
    await asyncio.sleep(0.05)
    assert heard == ["bir xil gap"]
    assert speech.synthesized == ["javob"]
    await session.hangup()
