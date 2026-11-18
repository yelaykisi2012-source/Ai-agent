"""Uzbek-only voice: spoken language, number pronunciation, TTS boundary.

Every test here is offline. The voice, the model and the speech engines are
fakes, so nothing depends on a network or on a live call.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from brain import AgentBrain, BrainUnavailable
from config import Settings
from fakes import FakeOpenAI, FakeSpeech, FakeTelegram, text_completion
from main import create_app
from runtime import AgentRuntime
from system_prompt import build_system_prompt, voice_rules
from telegram.voice_call import VoiceCallSession
from test_voice_call import FakeCallClient, FakeCallTransport, _call_settings
from test_voice_pipeline import _utterance_pcm
from voice.speech_engine import build_speech_engine
from voice.uzbek import (
    cardinal,
    is_uzbek,
    language_report,
    ordinal,
    phone_spoken,
    prepare_for_tts,
    voice_matches_language,
)

# --------------------------------------------------------------------------- #
# 1. The prompt: a call is Uzbek-only
# --------------------------------------------------------------------------- #
def test_call_prompt_demands_uzbek_and_overrides_the_language_rule(
    settings: Settings,
) -> None:
    prompt = build_system_prompt(settings, voice=True)

    assert voice_rules() in prompt
    assert "Every spoken answer is in Uzbek" in prompt
    # Rule 1 of the safety block says "reply in the language the person used";
    # on a call the Uzbek rule must win, and say so explicitly.
    assert "takes priority over rule 1" in prompt
    assert "Never switch to the language they used" in prompt


def test_chat_prompt_keeps_the_language_rule(settings: Settings) -> None:
    prompt = build_system_prompt(settings)

    assert "reply in the language the person used" in prompt
    assert "Every spoken answer is in Uzbek" not in prompt


def test_uzbek_answer_is_spoken_unchanged(settings: Settings) -> None:
    client = FakeOpenAI([text_completion("Ha, eshitaman. Bugun bandman.")])
    brain = AgentBrain(settings, client=client)

    reply = asyncio.run(brain.reply(history=[], user_message="Salom", voice_mode=True))

    assert reply.text == "Ha, eshitaman. Bugun bandman."
    assert len(client.requests) == 1  # no rewrite was needed


# --------------------------------------------------------------------------- #
# 2. A Russian or English answer is rewritten into Uzbek before it is spoken
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "foreign_answer",
    [
        "Привет! Как дела? Я тебя слышу.",
        "Hello! I can hear you fine, what do you need?",
    ],
    ids=["russian", "english"],
)
def test_foreign_answer_is_rewritten_into_uzbek(
    settings: Settings, foreign_answer: str, caplog
) -> None:
    client = FakeOpenAI([text_completion(foreign_answer), text_completion("Salom, eshitaman.")])
    brain = AgentBrain(settings, client=client)

    with caplog.at_level(logging.INFO, logger="brain"):
        reply = asyncio.run(
            brain.reply(history=[], user_message="Salom", voice_mode=True)
        )

    assert reply.text == "Salom, eshitaman."
    assert len(client.requests) == 2
    # The second request carries the rejected answer plus an Uzbek instruction.
    second = client.requests[1]["messages"]
    assert second[-2]["content"] == foreign_answer
    assert "o‘zbek tilida qayta yoz" in second[-1]["content"]
    assert client.requests[1].get("tools") is None
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "not Uzbek" in logged


def test_chat_mode_does_not_rewrite_a_russian_answer(settings: Settings) -> None:
    client = FakeOpenAI([text_completion("Привет! Как дела?")])
    brain = AgentBrain(settings, client=client)

    reply = asyncio.run(brain.reply(history=[], user_message="Привет"))

    assert reply.text == "Привет! Как дела?"
    assert len(client.requests) == 1


def test_answer_that_stays_foreign_is_dropped_not_spoken(settings: Settings) -> None:
    client = FakeOpenAI(
        [text_completion("Hello there, how are you"), text_completion("Sorry, I cannot help you")]
    )
    brain = AgentBrain(settings, client=client)

    with pytest.raises(BrainUnavailable):
        asyncio.run(brain.reply(history=[], user_message="Salom", voice_mode=True))
    assert len(client.requests) == 2


def test_a_turn_with_no_uzbek_answer_stays_silent(settings: Settings, caplog) -> None:
    client = FakeOpenAI(
        [text_completion("Привет, как дела?"), text_completion("Извини, я не понял")]
    )
    brain = AgentBrain(settings, client=client)
    runtime = AgentRuntime(
        settings, brain=brain, telegram=FakeTelegram(), speech=FakeSpeech()
    )
    app = create_app(settings, runtime=runtime)

    with TestClient(app):
        answer = asyncio.run(runtime.handle_call_message("Salom"))

    assert answer is None  # silence instead of a Russian sentence
    assert client.requests  # the rewrite really was attempted


# --------------------------------------------------------------------------- #
# 3. Numbers are pronounced, not read as digits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "spoken"),
    [
        (0, "nol"),
        (5, "besh"),
        (15, "o‘n besh"),
        (27, "yigirma yetti"),
        (105, "bir yuz besh"),
        (2026, "ikki ming yigirma olti"),
        (1500, "bir ming besh yuz"),
        (1000000, "bir million"),
        (-4, "minus to‘rt"),
    ],
)
def test_cardinal_numbers(value: int, spoken: str) -> None:
    assert cardinal(value) == spoken


@pytest.mark.parametrize(
    ("value", "spoken"),
    [
        (1, "birinchi"),
        (6, "oltinchi"),
        (9, "to‘qqizinchi"),
        (16, "o‘n oltinchi"),
        (20, "yigirmanchi"),
        (2026, "ikki ming yigirma oltinchi"),
    ],
)
def test_ordinal_numbers(value: int, spoken: str) -> None:
    assert ordinal(value) == spoken


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("15 daqiqa kuting", "o‘n besh daqiqa kuting"),
        ("27 kishi keldi", "yigirma yetti kishi keldi"),
        ("105 so‘m", "bir yuz besh so‘m"),
        ("2026 yilda", "ikki ming yigirma oltinchi yilda"),
        ("soat 12:30 da", "soat o‘n ikki yarim da"),
        ("12:30", "soat o‘n ikki yarim"),
        ("9:05", "soat to‘qqiz besh"),
        ("25% chegirma", "yigirma besh foiz chegirma"),
        ("1 500 so‘m", "bir ming besh yuz so‘m"),
        ("narxi 3,5 million", "narxi uch butun besh o‘ndan million"),
        ("harorat -3.5 daraja", "harorat minus uch butun besh o‘ndan daraja"),
        ("masofa 10 km", "masofa o‘n kilometr"),
        ("og‘irligi 5 kg", "og‘irligi besh kilogramm"),
        ("bugun 16.11.2026", "bugun o‘n oltinchi noyabr, ikki ming yigirma oltinchi yil"),
        ("2026-11-16", "o‘n oltinchi noyabr, ikki ming yigirma oltinchi yil"),
        ("16-noyabr", "o‘n oltinchi noyabr"),
        ("15 200 300 dona", "o‘n besh million ikki yuz ming uch yuz dona"),
    ],
)
def test_numbers_are_rewritten_for_speech(text: str, spoken: str) -> None:
    assert prepare_for_tts(text) == spoken


# --------------------------------------------------------------------------- #
# 4. Phone numbers keep every digit, in order, and are read one by one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("number", "spoken"),
    [
        (
            "+998901234567",
            "plyus to‘qqiz to‘qqiz sakkiz, to‘qqiz nol, bir ikki uch, to‘rt besh, olti yetti",
        ),
        ("998901234567", "to‘qqiz to‘qqiz sakkiz, to‘qqiz nol, bir ikki uch, to‘rt besh, olti yetti"),
    ],
)
def test_phone_numbers_are_read_digit_by_digit(number: str, spoken: str) -> None:
    assert phone_spoken(number) == spoken


def test_phone_number_in_a_sentence_keeps_all_digits() -> None:
    spoken = prepare_for_tts("Raqamingiz +998 90 123 45 67 ga qo‘ng‘iroq qildim.")

    assert spoken == (
        "Raqamingiz plyus to‘qqiz to‘qqiz sakkiz, to‘qqiz nol, bir ikki uch, "
        "to‘rt besh, olti yetti ga qo‘ng‘iroq qildim."
    )
    # Nothing was lost or reordered: the same eleven digits, same order.
    assert [d for d in "+998901234567"] == list("+998901234567")


def test_grouped_quantity_is_not_read_as_a_phone_number() -> None:
    assert prepare_for_tts("150 200 so‘m") == "bir yuz ellik ming ikki yuz so‘m"


# --------------------------------------------------------------------------- #
# 5. Names, brands and technical terms survive
# --------------------------------------------------------------------------- #
def test_names_and_technical_terms_are_not_rewritten() -> None:
    text = "Ksenus, Telegram va iPhone 15 orqali API ga ulandim, Ali bilan."

    spoken = prepare_for_tts(text)

    for kept in ("Ksenus", "Telegram", "iPhone", "API", "Ali"):
        assert kept in spoken
    assert "o‘n besh" in spoken  # only the number changed


def test_markdown_and_emoji_are_removed_before_speech() -> None:
    spoken = prepare_for_tts("**Salom!**\n- 15 daqiqa 👍\n- [havola](http://x.y)")

    assert spoken == "Salom! o‘n besh daqiqa havola"


# --------------------------------------------------------------------------- #
# 6. Language guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Salom, qalaysiz?",
        "Ha, ertaga soat o‘n ikki yarimda uchrashamiz.",
        "Men iPhone 15 oldim, Telegram orqali yuboraman.",
        "o‘n besh",  # digits only, after normalisation
    ],
)
def test_uzbek_text_passes_the_guard(text: str) -> None:
    assert is_uzbek(text)


@pytest.mark.parametrize(
    "text",
    [
        "Привет, как дела? Я тебя слышу.",
        "Sorry, I can help you with that tomorrow.",
        "Men sizga help you with the time and they said that.",
    ],
)
def test_foreign_text_fails_the_guard(text: str) -> None:
    report = language_report(text)

    assert not report.ok
    assert report.reasons


# --------------------------------------------------------------------------- #
# 7. TTS never receives another language
# --------------------------------------------------------------------------- #
def test_a_russian_reply_is_not_sent_to_tts(settings: Settings, caplog) -> None:
    asyncio.run(_speak_scenario(_call_settings(settings), "Привет, как дела?", caplog))


async def _speak_scenario(settings: Settings, text: str, caplog) -> None:
    speech = FakeSpeech()

    async def handler(_text: str) -> str:
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
    with caplog.at_level(logging.WARNING, logger="telegram.voice_call"):
        frames = await session._speak(text)
    await session.hangup()

    assert frames == 0
    assert speech.synthesized == []
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "not Uzbek" in logged or "nothing in this reply was Uzbek" in logged


def test_only_the_uzbek_sentence_of_a_mixed_reply_is_spoken(
    settings: Settings,
) -> None:
    asyncio.run(_mixed_reply_scenario(_call_settings(settings)))


async def _mixed_reply_scenario(settings: Settings) -> None:
    speech = FakeSpeech()

    async def handler(_text: str) -> str:
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
    await session._speak("Ertaga 15 daqiqada kelaman. Привет, как дела?")
    await session.hangup()

    assert speech.synthesized == ["Ertaga o‘n besh daqiqada kelaman."]


def test_uzbek_reply_reaches_tts_with_spoken_numbers(settings: Settings) -> None:
    asyncio.run(_normal_reply_scenario(_call_settings(settings)))


async def _normal_reply_scenario(settings: Settings) -> None:
    speech = FakeSpeech()

    async def handler(_text: str) -> str:
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
    await session._speak("Soat 12:30 da qo‘ng‘iroq qilaman. Narxi 1 500 so‘m.")
    await session.hangup()

    assert speech.synthesized == [
        "Soat o‘n ikki yarim da qo‘ng‘iroq qilaman.",
        "Narxi bir ming besh yuz so‘m.",
    ]
    assert all(fmt == "pcm" for fmt in speech.formats)  # frame contract untouched


# --------------------------------------------------------------------------- #
# 8. STT language hint and TTS voice check
# --------------------------------------------------------------------------- #
class _RecordingSpeech(FakeSpeech):
    def __init__(self, transcript: str = "Salom") -> None:
        super().__init__(transcript)
        self.languages: list[str | None] = []

    async def transcribe(
        self, audio: bytes, filename: str = "utterance.webm", language: str | None = None
    ) -> str:
        self.languages.append(language)
        return await super().transcribe(audio, filename=filename, language=language)


def test_stt_auto_detects_by_default(settings: Settings) -> None:
    assert asyncio.run(_stt_language_scenario(_call_settings(settings))) == [None]


def test_stt_language_is_passed_when_configured(settings: Settings) -> None:
    configured = _call_settings(settings).model_copy(update={"stt_language": "uz"})

    assert asyncio.run(_stt_language_scenario(configured)) == ["uz"]


async def _stt_language_scenario(settings: Settings) -> list[str | None]:
    speech = _RecordingSpeech("Salom")

    async def handler(_text: str) -> str:
        return ""  # the turn ends after transcription

    session = VoiceCallSession(
        settings,
        FakeCallTransport(),
        FakeCallClient(),
        peer_id=555,
        speech=speech,
        on_message=handler,
    )
    await session.place()
    await session._respond(_utterance_pcm(1), 1)
    await session.hangup()
    return speech.languages


def test_default_configuration_is_uzbek(settings: Settings) -> None:
    fresh = Settings(_env_file=None)  # type: ignore[arg-type]

    assert fresh.voice_language == "uz"
    assert fresh.stt_language == ""  # auto-detect: other languages are understood
    assert fresh.stt_language_hint is None
    assert voice_matches_language(fresh.edge_tts_voice, fresh.voice_language)


def test_a_non_uzbek_tts_voice_is_reported(settings: Settings, caplog) -> None:
    mismatched = settings.model_copy(
        update={"tts_provider": "edge", "edge_tts_voice": "ru-RU-DmitryNeural"}
    )

    with caplog.at_level(logging.WARNING, logger="voice.speech_engine"):
        build_speech_engine(mismatched)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "is not a uz voice" in logged
    assert "uz-UZ-MadinaNeural" in logged


def test_an_uzbek_tts_voice_is_not_warned_about(settings: Settings, caplog) -> None:
    matched = settings.model_copy(
        update={"tts_provider": "edge", "edge_tts_voice": "uz-UZ-SardorNeural"}
    )

    with caplog.at_level(logging.WARNING, logger="voice.speech_engine"):
        build_speech_engine(matched)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("is not a uz voice" in r.getMessage() for r in warnings)


def test_voice_locale_detection() -> None:
    assert voice_matches_language("uz-UZ-MadinaNeural", "uz")
    assert voice_matches_language("uz-UZ-SardorNeural", "UZ")
    assert not voice_matches_language("ru-RU-DmitryNeural", "uz")
    assert not voice_matches_language("", "uz")


def test_italian_answer_is_not_accepted_as_uzbek() -> None:
    from voice.uzbek import language_report

    report = language_report(
        "Ciao. Senza di chi o di cosa? Se hai voglia di raccontare, ascolto."
    )

    assert report.ok is False
    assert "foreign" in report.detail


def test_a_foreign_opener_is_stripped_not_spoken() -> None:
    from voice.uzbek import strip_foreign_openers

    assert strip_foreign_openers("Okay, albatta yuboraman.") == "albatta yuboraman."
    assert strip_foreign_openers("Ciao, salom.") == "salom."
    assert strip_foreign_openers("Okay.") == ""


def test_one_borrowed_word_does_not_silence_an_uzbek_sentence() -> None:
    from voice.uzbek import language_report

    assert language_report("Xo'p, ok, hoziroq yuboraman.").ok is True


def test_uzbek_words_that_look_french_are_not_flagged() -> None:
    from voice.uzbek import language_report

    # "un" (flour) and "et" (meat) are ordinary Uzbek words and must not be
    # read as French.
    assert language_report("Un va et oldim, non ham qo'shdim.").ok is True
