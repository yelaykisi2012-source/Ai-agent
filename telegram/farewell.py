"""Knowing that a spoken conversation is over.

The call pipeline hears a transcript, not an intention, so this module answers
one question: did the person just say goodbye? Two mistakes are possible and
they are not equally bad:

* hanging up while the person still wants to talk is a broken call, so the
  phrases here are unambiguous closings and a match never hangs up directly --
  the session says goodbye first and waits, and any new speech cancels it;
* staying on a dead call only costs a few seconds, because the call already
  has a duration watchdog and an idle timeout.

Questions are never goodbyes ("'xayr' nima degani?") — neither a question mark
nor a question word is enough to end a call — and a farewell buried in a long
sentence is not one either, so both are rejected.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

#: How many other words may surround a farewell phrase. More than this means
#: the word appeared inside a longer thought, not as a closing.
_MAX_EXTRA_WORDS = 6

#: Short Uzbek goodbyes Ksenus says before ending the call. Picking one at
#: random keeps three calls in a row from sounding identical.
GOODBYES = (
    "Xayr, salomat bo‘ling!",
    "Xo‘p, ko‘rishguncha!",
    "Yaxshi, bog‘lanamiz. Xayr!",
)

#: "Call me in N minutes" on a live call. The number is minutes; the words are
#: what a person actually says ("3 daqiqadan keyin qo‘ng‘iroq qil").
_CALLBACK_MINUTES = re.compile(
    r"(\d{1,3})\s*(?:daqiqa|minut|min)", re.IGNORECASE
)
_CALLBACK_WORDS = re.compile(
    r"qo[‘'ʻ]?ng[‘'ʻ]?iroq|звон|перезвон|call me|call back", re.IGNORECASE
)
_CALLBACK_LATER = re.compile(
    r"keyin|keyinroq|so[‘'ʻ]ng|после|через|later|again", re.IGNORECASE
)


def callback_delay_minutes(text: str) -> int | None:
    """Minutes until the person wants to be called back, or None.

    This is deliberately narrow: it only fires when the sentence asks for a
    call *and* names a later time (or says "later"). A plain "qo‘ng‘iroq qil"
    during the call means "keep talking", not "call me again".
    """
    body = (text or "").strip()
    if not body or not _CALLBACK_WORDS.search(body):
        return None
    match = _CALLBACK_MINUTES.search(body)
    if match is not None:
        minutes = int(match.group(1))
        return minutes if 1 <= minutes <= 720 else None
    if not _CALLBACK_LATER.search(body):
        return None
    lowered = body.lower()
    if "yarim soat" in lowered:
        return 30
    hour = re.search(r"(\d{1,2})\s*soat", lowered)
    if hour is not None:
        minutes = int(hour.group(1)) * 60
        return minutes if 1 <= minutes <= 720 else None
    if "soat" in lowered:
        return 60
    # "keyinroq qo‘ng‘iroq qil" without a number: soon, not immediately.
    return 5

#: Unambiguous closings, longest first so "xayr endi" wins over "xayr".
#: ``language`` is only used for logs.
_FAREWELLS: tuple[tuple[str, str], ...] = (
    # Uzbek
    ("suhbatni tugatamiz", "uz"),
    ("suhbatni yakunlaymiz", "uz"),
    ("qo‘ng‘iroqni yakunla", "uz"),
    ("qo‘ng‘iroqni tugat", "uz"),
    ("qo‘ng‘iroqni o‘chir", "uz"),
    ("ko‘rishguncha", "uz"),
    ("ko‘rishgungacha", "uz"),
    ("salomat bo‘ling", "uz"),
    ("omon bo‘ling", "uz"),
    ("xayr endi", "uz"),
    ("endi xayr", "uz"),
    ("xayr", "uz"),
    ("men ketdim", "uz"),
    ("men chiqaman", "uz"),
    ("qo‘ng‘iroqni uz", "uz"),
    # Russian
    ("до свидания", "ru"),
    ("закончи разговор", "ru"),
    ("заверши звонок", "ru"),
    ("прощай", "ru"),
    ("пока", "ru"),
    # English
    ("end the call", "en"),
    ("hang up", "en"),
    ("goodbye", "en"),
    ("bye bye", "en"),
    ("see you", "en"),
    ("bye", "en"),
)

#: Words that feel like a closing but are not one on their own. They are
#: deliberately NOT enough to end a call: "bo‘ldi" also means "done/ready".
_NOT_FAREWELLS = frozenset(
    {
        "ha",
        "yo‘q",
        "xo‘p",
        "ok",
        "okay",
        "mayli",
        "rahmat",
        "tashakkur",
        "bo‘ldi",
        "tamom",
        "yaxshi",
        "keyinroq",
        "салом",
        "salom",
        "hello",
        "привет",
    }
)

_WORD_RE = re.compile(r"[\w‘']+", re.UNICODE)

#: Question words. Asking something is not leaving, even when the sentence
#: contains a farewell word — "«xayr» nima degani" has no question mark in
#: speech-to-text output, so the words themselves have to be checked too.
_QUESTION_WORDS = frozenset(
    {
        # Uzbek
        "nima",
        "qanday",
        "qachon",
        "kim",
        "nega",
        "qayerda",
        "qaysi",
        "necha",
        "qancha",
        # Russian
        "что",
        "как",
        "почему",
        "зачем",
        "когда",
        "кто",
        "где",
        "какой",
        "сколько",
        # English
        "what",
        "why",
        "how",
        "when",
        "who",
        "where",
        "which",
    }
)


@dataclass(frozen=True)
class Farewell:
    """A detected goodbye."""

    phrase: str
    language: str
    transcript: str
    goodbye: str

    @property
    def detail(self) -> str:
        return f"{self.language}:{self.phrase}"


def goodbye_line() -> str:
    """The short Uzbek sentence spoken before the call is ended."""
    return random.choice(GOODBYES)


def _normalize(transcript: str) -> str:
    """Lowercase, unify the two apostrophes, drop punctuation, collapse spaces."""
    text = (transcript or "").lower()
    text = text.replace("'", "‘").replace("ʻ", "‘").replace("’", "‘")
    text = re.sub(r"[^\w‘\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def is_farewell_word(word: str) -> bool:
    """True when a single word is a goodbye and not just a polite closer."""
    normalized = _normalize(word)
    if not normalized or normalized in _NOT_FAREWELLS:
        return False
    return any(phrase == normalized for phrase, _ in _FAREWELLS)


def farewell_match(transcript: str) -> Farewell | None:
    """Return the goodbye in ``transcript``, or ``None`` when there is none.

    A question is never a goodbye, and neither is a farewell word that is only
    a small part of a long sentence.
    """
    raw = (transcript or "").strip()
    if not raw or "?" in raw or "؟" in raw or "？" in raw:
        return None
    text = _normalize(raw)
    if not text:
        return None
    words = text.split()
    if any(word in _QUESTION_WORDS for word in words):
        return None
    for phrase, language in _FAREWELLS:
        if not re.search(rf"(?<![\w‘]){re.escape(phrase)}(?![\w‘])", text):
            continue
        extra = len(words) - len(phrase.split())
        if extra > _MAX_EXTRA_WORDS:
            # "Xayr" inside a long sentence is part of a thought, not a closing.
            continue
        return Farewell(
            phrase=phrase,
            language=language,
            transcript=raw,
            goodbye=goodbye_line(),
        )
    return None
