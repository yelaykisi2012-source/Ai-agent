"""Automatic call ending — mock tests.

Everything here runs offline: the farewell rules are checked as pure functions,
and the call itself is driven through the real session code with a fake audio
transport and a fake Telethon-shaped client. No Telegram account, no network, no
real audio. What still needs a real phone call is listed separately in
``scripts/live_call_check.py`` and in the report, not pretended to be covered
here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from config import Settings
from fakes import FakeSpeech

# The call fakes are the ones the existing call tests already proved correct:
# reusing them keeps one definition of the transport contract instead of two.
from test_voice_call import (
    FakeCallClient,
    FakeCallTransport,
    _accepted,
    _loud_frame,
    _wait_for,
)
from telegram.audio import SILENCE_FRAME
from telegram.farewell import GOODBYES, farewell_match, is_farewell_word
from telegram.voice_call import VoiceCallSession


class ScriptedSpeech(FakeSpeech):
    """Returns a prepared transcript per utterance instead of the same one."""

    def __init__(self, transcripts: list[str]) -> None:
        super().__init__(transcript=transcripts[0] if transcripts else "")
        self.scripts = list(transcripts)

    async def transcribe(
        self, audio: bytes, filename: str = "utterance.webm", language: str | None = None
    ) -> str:
        self.transcribed.append(audio)
        return self.scripts.pop(0) if self.scripts else ""


def _call_settings(settings: Settings) -> Settings:
    settings.telegram_calls_enabled = True
    settings.telegram_owner_user_id = 555
    settings.telegram_call_auto_answer = True
    # The real default is 3 s; a short grace keeps the test fast. The rule being
    # tested (speak, wait, then end) is unchanged.
    settings.telegram_call_goodbye_grace_seconds = 0.05
    return settings


def _say(session: VoiceCallSession, *, loud: int = 60, silence: int = 110) -> None:
    """One utterance: a second of voice, then enough silence to close it."""
    for _ in range(loud):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", _loud_frame())
    for _ in range(silence):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)


async def _utter(
    session: VoiceCallSession, *, turn: int = 0, loud: int = 60, silence: int = 110
) -> None:
    """The same utterance, in the order a real caller produces it.

    Frames are pushed one at a time with the loop running in between, and the
    caller waits until Ksenus has stopped talking. That is what happens on a
    real call, and without it the voice frames land inside the echo/barge-in
    window and are dropped before they ever reach the VAD.

    ``turn`` varies the audio: the session caches a transcript per audio digest,
    so byte-identical utterances would reuse the first transcript instead of
    being transcribed again.
    """
    assert await _wait_for(lambda: not session._speaking), "Ksenus kept talking"
    frame = _loud_frame(5000 + turn * 137)
    for _ in range(loud):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", frame)
        await asyncio.sleep(0)
    for _ in range(silence):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)
        await asyncio.sleep(0)


async def _connected_session(
    settings: Settings, speech: FakeSpeech, on_message: Any, on_finished: Any = None
) -> tuple[VoiceCallSession, FakeCallTransport, FakeCallClient]:
    transport = FakeCallTransport()
    client = FakeCallClient()
    session = VoiceCallSession(
        settings,
        transport,
        client,
        peer_id=555,
        speech=speech,
        on_message=on_message,
        on_finished=on_finished,
    )
    await session.place()
    await session.handle_phone_call(_accepted())
    # The greeting is spoken first; wait for it so the caller's own audio is not
    # swallowed by the barge-in grace.
    assert await _wait_for(lambda: speech.synthesized and not session._speaking)
    return session, transport, client


# --------------------------------------------------------------------------- #
# 1. Farewell phrases (unit level, no call)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "transcript",
    [
        # Uzbek, including the forms the task named explicitly
        "xayr",
        "Xayr!",
        "ko‘rishguncha",
        "suhbatni tugatamiz",
        "qo‘ng‘iroqni yakunla",
        "salomat bo‘ling",
        "endi xayr",
        "men ketdim",
        "Xo‘p, ko‘rishguncha",
        # the same words typed with a straight apostrophe
        "ko'rishguncha",
        "qo'ng'iroqni yakunla",
        # Russian
        "до свидания",
        "пока",
        "заверши звонок",
        # English
        "goodbye",
        "bye",
        "end the call",
        "hang up",
        "see you",
    ],
)
def test_closings_in_every_supported_language_are_recognised(transcript: str) -> None:
    farewell = farewell_match(transcript)
    assert farewell is not None, transcript
    assert farewell.language in {"uz", "ru", "en"}
    assert farewell.goodbye in GOODBYES


@pytest.mark.parametrize(
    "transcript",
    [
        "",
        "   ",
        "ha",
        "yo‘q",
        "xo‘p",
        "ok",
        "mayli",
        "rahmat",
        "bo‘ldi",
        "tamom",
        "yaxshi",
        "salom",
        "hello",
        "привет",
        "bugun ob-havo yaxshi",
        "meni eshityapsanmi",
        # a question about the word is not the word
        "xayr nima degani?",
        "«xayr» degani nima",
        "should I hang up now?",
        # the word inside a longer thought
        "men bugun bozordan qaytayotganimda xayr degan odamni uchratdim va u bilan gaplashdim",
        # a word that only looks like a farewell
        "xayrli ishlatish",
        "xayriya",
    ],
)
def test_ordinary_words_questions_and_long_sentences_are_not_closings(
    transcript: str,
) -> None:
    assert farewell_match(transcript) is None, transcript


def test_a_single_farewell_word_is_enough_but_a_polite_closer_is_not() -> None:
    assert is_farewell_word("xayr")
    assert is_farewell_word("ko‘rishguncha")
    assert not is_farewell_word("bo‘ldi")
    assert not is_farewell_word("rahmat")
    assert not is_farewell_word("ha")


# --------------------------------------------------------------------------- #
# 2. A recognised closing ends the call once, cleanly
# --------------------------------------------------------------------------- #
def test_a_farewell_is_answered_with_a_goodbye_and_then_the_call_ends(
    settings: Settings,
) -> None:
    asyncio.run(_farewell_ends_call(_call_settings(settings)))


async def _farewell_ends_call(settings: Settings) -> None:
    heard: list[str] = []
    finished: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return "Bu javob aytilmasligi kerak."

    speech = ScriptedSpeech(["Xayr, ko‘rishguncha!"])
    session, transport, client = await _connected_session(
        settings,
        speech,
        handler,
        lambda _s, reason, _d: finished.append(reason),
    )

    _say(session)

    assert await _wait_for(lambda: bool(finished)), "the call did not end"
    # A goodbye is not a model turn: the brain was never asked.
    assert heard == []
    assert finished == ["farewell"]
    # ``_speak`` synthesizes sentence by sentence, so the goodbye may arrive as
    # two chunks ("Yaxshi, bog‘lanamiz." + "Xayr!"); joined, it is one line.
    spoken = " ".join(speech.synthesized)
    assert any(line in spoken for line in GOODBYES), speech.synthesized
    assert session.status()["goodbyes"] == 1
    assert session.status()["goodbye_pending"] is False
    assert session.status()["state"] == "ended"
    # Cleaned up exactly once: one discard request, transport stopped.
    assert len(client.of_type("DiscardCallRequest")) == 1
    assert transport.stopped == [555]
    assert session._goodbye_task is None


# --------------------------------------------------------------------------- #
# 3. An ordinary pause does not drop the call
# --------------------------------------------------------------------------- #
def test_an_ordinary_pause_keeps_the_call_alive(settings: Settings) -> None:
    asyncio.run(_pause_keeps_call(_call_settings(settings)))


async def _pause_keeps_call(settings: Settings) -> None:
    heard: list[str] = []
    finished: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return "Ha, eshitaman."

    speech = ScriptedSpeech(["Meni eshityapsanmi", "", "", ""])
    session, _transport, client = await _connected_session(
        settings, speech, handler, lambda _s, reason, _d: finished.append(reason)
    )

    await _utter(session)
    assert await _wait_for(lambda: heard == ["Meni eshityapsanmi"])
    assert await _wait_for(lambda: not session._speaking)

    # Four seconds of nothing but silence frames, the way a real pause looks.
    for _ in range(400):
        session.push_frames(555, "PLAYBACK", "MICROPHONE", SILENCE_FRAME)
        await asyncio.sleep(0)
    await asyncio.sleep(0.3)

    assert finished == [], f"a pause ended the call: {finished}"
    assert session.connected is True
    assert session.status()["goodbye_pending"] is False
    assert session.status()["goodbyes"] == 0
    assert client.of_type("DiscardCallRequest") == []

    await session.hangup()
    assert finished == ["owner_hangup"]


def test_a_continuing_conversation_keeps_the_call_alive(settings: Settings) -> None:
    asyncio.run(_conversation_continues(_call_settings(settings)))


async def _conversation_continues(settings: Settings) -> None:
    heard: list[str] = []
    finished: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return f"Javob: {text}"

    speech = ScriptedSpeech(
        ["Salom", "Ishlar qanday", "Rahmat", "Bo‘ldi", "Ertaga nima qilamiz"]
    )
    session, _transport, client = await _connected_session(
        settings, speech, handler, lambda _s, reason, _d: finished.append(reason)
    )

    for index in range(5):
        await _utter(session, turn=index)
        assert await _wait_for(lambda: len(heard) == index + 1), heard
        assert await _wait_for(lambda: not session._speaking)

    assert len(heard) == 5, f"not every turn reached the brain: {heard}"
    assert finished == [], f"the conversation was cut: {finished}"
    assert session.connected is True
    assert client.of_type("DiscardCallRequest") == []

    await session.hangup()
    assert finished == ["owner_hangup"]


# --------------------------------------------------------------------------- #
# 4. The caller changed their mind: the ending is cancelled
# --------------------------------------------------------------------------- #
def test_speaking_again_cancels_the_pending_goodbye(settings: Settings) -> None:
    settings = _call_settings(settings)
    # A longer grace gives the test room to interrupt, exactly like a real call
    # where the person says "wait, one more thing" after the goodbye.
    settings.telegram_call_goodbye_grace_seconds = 1.0
    asyncio.run(_goodbye_cancelled(settings))


async def _goodbye_cancelled(settings: Settings) -> None:
    heard: list[str] = []
    finished: list[str] = []

    async def handler(text: str) -> str:
        heard.append(text)
        return "Qanday savol?"

    speech = ScriptedSpeech(["Xayr", "To‘xtang, yana bir savolim bor"])
    session, _transport, client = await _connected_session(
        settings, speech, handler, lambda _s, reason, _d: finished.append(reason)
    )

    await _utter(session)  # "Xayr": the goodbye starts, the call is still open
    assert await _wait_for(lambda: session.status()["goodbye_pending"]), (
        "no goodbye was started"
    )

    # The caller speaks again while the goodbye is waiting out its grace window.
    await _utter(session, turn=1)
    assert await _wait_for(lambda: heard == ["To‘xtang, yana bir savolim bor"])

    await asyncio.sleep(1.3)
    assert finished == [], f"the cancelled goodbye still hung up: {finished}"
    assert session.connected is True
    assert session.status()["goodbye_pending"] is False
    assert session.status()["goodbyes_cancelled"] >= 1
    assert client.of_type("DiscardCallRequest") == []

    await session.hangup()
    assert finished == ["owner_hangup"]
    assert len(client.of_type("DiscardCallRequest")) == 1


def test_a_second_farewell_while_a_goodbye_runs_does_not_end_the_call_twice(
    settings: Settings,
) -> None:
    settings = _call_settings(settings)
    # 30 s: the flow cannot finish on its own, so anything that ends the call
    # here would be a second, unwanted hang-up.
    settings.telegram_call_goodbye_grace_seconds = 30.0
    asyncio.run(_double_goodbye(settings))


async def _double_goodbye(settings: Settings) -> None:
    speech = FakeSpeech(transcript="Xayr")
    client = FakeCallClient()
    finished: list[str] = []
    session = VoiceCallSession(
        settings,
        FakeCallTransport(),
        client,
        peer_id=555,
        speech=speech,
        on_message=lambda text: asyncio.sleep(0, result="ok"),
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )
    await session.place()

    first = farewell_match("Xayr")
    assert first is not None
    session._begin_goodbye(first)
    running = session._goodbye_task
    assert running is not None

    second = farewell_match("Ko‘rishguncha")
    assert second is not None
    session._begin_goodbye(second)

    assert session._goodbye_task is running, "a second goodbye flow was started"
    assert session.status()["goodbyes"] == 1
    assert session.status()["goodbye_pending"] is True

    await session.hangup()
    assert finished == ["owner_hangup"]
    assert len(client.of_type("DiscardCallRequest")) == 1
    assert session.status()["goodbye_pending"] is False
    assert session._goodbye_task is None
    assert running.done()


def test_talking_over_the_goodbye_also_cancels_it(settings: Settings) -> None:
    """The other cancellation path: the caller speaks while we are saying bye."""
    asyncio.run(_barge_in_cancels_goodbye(_call_settings(settings)))


async def _barge_in_cancels_goodbye(settings: Settings) -> None:
    settings.telegram_call_goodbye_grace_seconds = 30.0
    finished: list[str] = []
    client = FakeCallClient()
    session = VoiceCallSession(
        settings,
        FakeCallTransport(),
        client,
        peer_id=555,
        speech=FakeSpeech(transcript="Xayr"),
        on_message=lambda text: asyncio.sleep(0, result=""),
        on_finished=lambda _s, reason, _d: finished.append(reason),
    )
    await session.place()

    farewell = farewell_match("Xayr")
    assert farewell is not None
    session._begin_goodbye(farewell)
    assert session.status()["goodbye_pending"] is True

    # Our own goodbye is still playing when the caller's voice comes back.
    session._speaking = True
    session._outbox.put_nowait(_loud_frame())
    session._barge_in()

    assert session.status()["goodbye_pending"] is False
    assert session.status()["goodbyes_cancelled"] == 1
    assert finished == [], "a barge-in must not end the call"
    assert client.of_type("DiscardCallRequest") == []

    await session.hangup()
    assert finished == ["owner_hangup"]
