"""Text preparation for Uzbek speech.

Two jobs, both on the text side only. Nothing here touches audio formats,
sample rates or the Telegram transport contract:

* ``prepare_for_tts`` rewrites digits the way a person says them on the phone
  ("15" -> "o‘n besh", "12:30" -> "soat o‘n ikki yarim", "+998901234567" ->
  digit by digit), because a TTS voice has no Uzbek number rules of its own;
* ``language_report`` says whether a reply is really Uzbek, so a Russian or
  English answer is never handed to the Uzbek voice as if that were fine.

The module never translates anything: choosing words is the model's job. This
only makes the model's Uzbek text pronounceable and checks that it is Uzbek.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Uzbek Latin writes o‘ and g‘ with U+2018 and the apostrophe with U+2019.
#: Both characters were verified against the live Edge TTS uz-UZ voices.
_TICK = "\u2018"

# --------------------------------------------------------------------------- #
# Number words
# --------------------------------------------------------------------------- #
_ONES = (
    "",
    "bir",
    "ikki",
    "uch",
    f"to{_TICK}rt",
    "besh",
    "olti",
    "yetti",
    "sakkiz",
    f"to{_TICK}qqiz",
)
_TENS = (
    "",
    f"o{_TICK}n",
    "yigirma",
    "otuz",
    "qirq",
    "ellik",
    "oltmish",
    "yetmish",
    "sakson",
    f"to{_TICK}qson",
)
_SCALES = ((10**12, "trillion"), (10**9, "milliard"), (10**6, "million"), (10**3, "ming"))
_DIGIT_WORDS = {
    "0": "nol",
    "1": "bir",
    "2": "ikki",
    "3": "uch",
    "4": f"to{_TICK}rt",
    "5": "besh",
    "6": "olti",
    "7": "yetti",
    "8": "sakkiz",
    "9": f"to{_TICK}qqiz",
}
_MONTHS = {
    1: "yanvar",
    2: "fevral",
    3: "mart",
    4: "aprel",
    5: "may",
    6: "iyun",
    7: "iyul",
    8: "avgust",
    9: "sentabr",
    10: "oktabr",
    11: "noyabr",
    12: "dekabr",
}
_MONTH_NAMES = "|".join(sorted(_MONTHS.values(), key=len, reverse=True))
_VOWELS = frozenset("aeiou")
#: Fraction denominators, by how many digits follow the comma.
_FRACTIONS = {1: f"o{_TICK}ndan", 2: "yuzdan", 3: "mingdan"}
_CURRENCIES = {
    f"so{_TICK}m": f"so{_TICK}m",
    "so'm": f"so{_TICK}m",
    "soʻm": f"so{_TICK}m",
    "som": f"so{_TICK}m",
    "сўм": f"so{_TICK}m",
    "сум": f"so{_TICK}m",
    "usd": "dollar",
    "eur": "yevro",
    "rub": "rubl",
    "rubl": "rubl",
    "kzt": "tenge",
}
_CURRENCY_SYMBOLS = {"$": "dollar", "€": "yevro", "₽": "rubl"}
_UNITS = {
    "km": "kilometr",
    "sm": "santimetr",
    "cm": "santimetr",
    "mm": "millimetr",
    "kg": "kilogramm",
    "mg": "milligramm",
    "ml": "millilitr",
    "ga": "gektar",
    "m": "metr",
    "g": "gramm",
    "l": "litr",
    "t": "tonna",
}
#: Longest first, so "km" is never read as "k" plus a leftover "m".
_UNIT_NAMES = "|".join(sorted(_UNITS, key=len, reverse=True))

_GROUPED_DIGITS = r"\d{1,3}(?:[ \u00a0]\d{3})+"
_AMOUNT = rf"(?:{_GROUPED_DIGITS}|\d+(?:[.,]\d+)?)"

_ISO_DATE_RE = re.compile(
    r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b(?P<tail>\s*yil\w*)?"
)
_DMY_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./](?P<year>\d{4})\b(?P<tail>\s*yil\w*)?"
)
_YEAR_SUFFIX_RE = re.compile(r"\b(?P<year>\d{4})\s*-?\s*yil", re.IGNORECASE)
_DAY_MONTH_RE = re.compile(
    rf"\b(?P<day>\d{{1,2}})\s*-\s*(?P<month>{_MONTH_NAMES})\b", re.IGNORECASE
)
_PHONE_RE = re.compile(r"(?<![\w.+])\+?\d(?:[\d\s().-]{5,17})\d(?![\w-])")
_TIME_RE = re.compile(
    r"(?P<prefix>soat\s+)?\b(?P<hour>\d{1,2}):(?P<minute>\d{2})\b", re.IGNORECASE
)
_PERCENT_RE = re.compile(rf"\b(?P<value>{_AMOUNT})\s*%")
_TEMP_RE = re.compile(rf"(?P<value>-?{_AMOUNT})\s*(?:°\s*C\b|daraja\b)")
_MONEY_RE = re.compile(
    rf"\b(?P<value>{_AMOUNT})\s*(?P<currency>so{_TICK}m|so'm|soʻm|som|сўм|сум|"
    r"USD|EUR|RUB|rubl|KZT)\b",
    re.IGNORECASE,
)
_SYMBOL_MONEY_RE = re.compile(
    rf"(?P<symbol>[$€₽])\s*(?P<value>{_AMOUNT})|(?P<value2>{_AMOUNT})\s*(?P<symbol2>[$€₽])"
)
_UNIT_RE = re.compile(rf"\b(?P<value>{_AMOUNT})\s*(?P<unit>{_UNIT_NAMES})\b(?![\w])")
_GROUPED_RE = re.compile(rf"\b(?P<value>{_GROUPED_DIGITS})\b")
_DECIMAL_RE = re.compile(r"(?<![\d.,])(?P<whole>\d+)[.,](?P<frac>\d+)(?![\d.,])")
_INTEGER_RE = re.compile(r"\b(?P<value>\d+)\b")
_LEFTOVER_DIGITS_RE = re.compile(r"\d")


def _under_thousand(number: int) -> str:
    hundreds, rest = divmod(number, 100)
    words: list[str] = []
    if hundreds:
        words.append(f"{_ONES[hundreds]} yuz")
    if rest >= 10:
        tens, ones = divmod(rest, 10)
        words.append(_TENS[tens])
        if ones:
            words.append(_ONES[ones])
    elif rest:
        words.append(_ONES[rest])
    return " ".join(words)


def cardinal(number: int) -> str:
    """Return the Uzbek words for an integer: 105 -> "bir yuz besh"."""
    if number == 0:
        return _DIGIT_WORDS["0"]
    if number < 0:
        return f"minus {cardinal(-number)}"
    if number >= 10**15:
        # An id that long is a code, not a quantity: read it digit by digit.
        return digits_spoken(str(number))
    words: list[str] = []
    remainder = number
    for value, name in _SCALES:
        count, remainder = divmod(remainder, value)
        if count:
            words.append(f"{cardinal(count)} {name}")
    if remainder:
        words.append(_under_thousand(remainder))
    return " ".join(words)


def ordinal(number: int) -> str:
    """Return the Uzbek ordinal: 16 -> "o‘n oltinchi", 2026 -> "... oltinchi"."""
    word = cardinal(number)
    suffix = "nchi" if word[-1].lower() in _VOWELS else "inchi"
    return f"{word}{suffix}"


def digits_spoken(text: str) -> str:
    """Read every digit separately, the way people dictate a code or number."""
    words: list[str] = []
    for character in text:
        if character in _DIGIT_WORDS:
            words.append(_DIGIT_WORDS[character])
        elif character == "+":
            words.append("plyus")
    return " ".join(words)


def phone_spoken(raw: str) -> str:
    """Read a phone number digit by digit, grouped so the voice pauses.

    A phone number is never a quantity, so "+998901234567" must not become a
    single huge number word. Uzbek mobile numbers are dictated 998 / 90 /
    123 / 45 / 67; any other shape is read in pairs.
    """
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    groups: list[str] = []
    if digits.startswith("998") and len(digits) == 12:
        head, rest = digits[:3], digits[3:]
        groups.append(" ".join(_DIGIT_WORDS[d] for d in head))
        index = 0
        for size in (2, 3, 2, 2):
            groups.append(" ".join(_DIGIT_WORDS[d] for d in rest[index : index + size]))
            index += size
    else:
        for index in range(0, len(digits), 2):
            groups.append(" ".join(_DIGIT_WORDS[d] for d in digits[index : index + 2]))
    prefix = "plyus " if raw.lstrip().startswith("+") else ""
    return f"{prefix}{', '.join(groups)}"


def _amount_spoken(raw: str) -> str:
    """Speak a quantity that may be grouped ("1 500") or fractional ("3,5")."""
    compact = re.sub(r"[ \u00a0]", "", raw)
    if re.fullmatch(r"-?\d+[.,]\d+", compact):
        return _decimal_spoken(compact)
    return cardinal(int(compact))


def _decimal_spoken(raw: str) -> str:
    sign = "minus " if raw.startswith("-") else ""
    body = raw.lstrip("-")
    whole, _, frac = body.partition(".")
    if not whole:
        whole, _, frac = body.partition(",")
    denominator = _FRACTIONS.get(len(frac), _FRACTIONS[1])
    return f"{sign}{cardinal(int(whole))} butun {cardinal(int(frac))} {denominator}"


def _time_spoken(hour: int, minute: int, *, prefix: str = "soat ") -> str:
    if minute == 0:
        return f"{prefix}{cardinal(hour)}"
    if minute == 30:
        return f"{prefix}{cardinal(hour)} yarim"
    return f"{prefix}{cardinal(hour)} {cardinal(minute)}"


def _date_spoken(day: int, month: int, year: int, *, with_year_word: bool = True) -> str:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        # Not a recognisable date: keep the values, read them as plain numbers.
        return f"{cardinal(day)} {cardinal(month)} {cardinal(year)}"
    spoken = f"{ordinal(day)} {_MONTHS[month]}"
    if with_year_word:
        spoken = f"{spoken}, {ordinal(year)} yil"
    else:
        spoken = f"{spoken}, {ordinal(year)}"
    return spoken


def _date_match(match: re.Match[str]) -> str:
    """Speak a written date, keeping an existing "yil" word instead of doubling it."""
    tail = match["tail"] or ""
    spoken = _date_spoken(
        int(match["day"]), int(match["month"]), int(match["year"]), with_year_word=not tail
    )
    return f"{spoken}{tail}"


def _phone_match(match: re.Match[str]) -> str:
    """Speak a phone number digit by digit, but only when it really is one.

    Two shapes look alike and mean different things: "8 903 123 45 67" is a
    number to dial, while "15 200 300" is a quantity. A leading "+" or a
    separator makes it a phone number; a bare digit run only counts as one
    from ten digits up, so "1000000" stays "bir million".
    """
    raw = match.group(0)
    stripped = raw.strip()
    digits = re.sub(r"\D", "", raw)
    has_plus = stripped.startswith("+")
    grouped_quantity = re.fullmatch(rf"{_GROUPED_DIGITS}", stripped) is not None
    if grouped_quantity and not has_plus:
        return cardinal(int(digits))
    separated = bool(re.search(r"[\s()+.\-]", stripped))
    if not has_plus and not separated and len(digits) < 10:
        return raw
    if not 7 <= len(digits) <= 15:
        return raw
    return phone_spoken(stripped)


# --------------------------------------------------------------------------- #
# Public text pipeline
# --------------------------------------------------------------------------- #
def normalize_numbers(text: str) -> str:
    """Rewrite every number in ``text`` into spoken Uzbek.

    The rules run from the most specific to the most general, so a date is not
    eaten by the decimal rule and a phone number is not read as a quantity.
    Values are only ever spelled out, never changed.
    """
    if not text or not _LEFTOVER_DIGITS_RE.search(text):
        return text

    result = _ISO_DATE_RE.sub(_date_match, text)
    result = _DMY_DATE_RE.sub(_date_match, result)
    result = _DAY_MONTH_RE.sub(
        lambda m: f"{ordinal(int(m['day']))} {m['month'].lower()}", result
    )
    result = _YEAR_SUFFIX_RE.sub(lambda m: f"{ordinal(int(m['year']))} yil", result)
    result = _PHONE_RE.sub(_phone_match, result)
    result = _TIME_RE.sub(
        lambda m: _time_spoken(
            int(m["hour"]), int(m["minute"]), prefix=m["prefix"] or "soat "
        ),
        result,
    )
    result = _PERCENT_RE.sub(
        lambda m: f"{_amount_spoken(m['value'])} foiz", result
    )
    result = _TEMP_RE.sub(
        lambda m: f"{_amount_spoken(m['value'])} daraja", result
    )
    result = _MONEY_RE.sub(
        lambda m: f"{_amount_spoken(m['value'])} {_CURRENCIES[m['currency'].lower()]}",
        result,
    )
    result = _SYMBOL_MONEY_RE.sub(_symbol_money, result)
    result = _UNIT_RE.sub(
        lambda m: f"{_amount_spoken(m['value'])} {_UNITS[m['unit'].lower()]}", result
    )
    result = _GROUPED_RE.sub(
        lambda m: cardinal(int(re.sub(r"[ \u00a0]", "", m["value"]))), result
    )
    result = _DECIMAL_RE.sub(lambda m: _decimal_spoken(f"{m['whole']}.{m['frac']}"), result)
    result = _INTEGER_RE.sub(lambda m: cardinal(int(m["value"])), result)
    return result


def _symbol_money(match: re.Match[str]) -> str:
    symbol = match["symbol"] or match["symbol2"]
    value = match["value"] or match["value2"]
    currency = _CURRENCY_SYMBOLS[symbol]
    amount = _amount_spoken(value)
    return f"{amount} {currency}"


_MARKDOWN_LINK_RE = re.compile(r"!?\[(?P<text>[^\]]*)\]\([^)]*\)")
_INLINE_CODE_RE = re.compile(r"`{1,3}[^`]*`{1,3}")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)")
_EMOJI_CATEGORIES = frozenset({"So", "Sk", "Cs", "Co"})
_INVISIBLE = frozenset({"\u200b", "\u200c", "\u200d", "\ufe0f", "\ufeff"})


def strip_markup(text: str) -> str:
    """Remove markdown, bullets and emoji: none of it can be spoken."""
    result = _MARKDOWN_LINK_RE.sub(lambda m: m["text"], text)
    result = _INLINE_CODE_RE.sub(lambda m: m.group(0).strip("`"), result)
    result = _HEADING_RE.sub("", result)
    result = _LIST_MARKER_RE.sub("", result)
    result = _EMPHASIS_RE.sub("", result)
    kept: list[str] = []
    for character in result:
        if character in _INVISIBLE:
            continue
        if character in ("\n", "\r", "\t"):
            kept.append(" ")
            continue
        if unicodedata.category(character) in _EMOJI_CATEGORIES:
            continue
        kept.append(character)
    return "".join(kept)


def prepare_for_tts(text: str) -> str:
    """Return ``text`` ready for the Uzbek voice: no markup, spoken numbers."""
    cleaned = strip_markup(text or "")
    cleaned = normalize_numbers(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def unread_digits(text: str) -> list[str]:
    """Digit runs still present after normalisation (a check, not an error)."""
    return _LEFTOVER_DIGITS_RE.findall(text or "")


# --------------------------------------------------------------------------- #
# Language guard
# --------------------------------------------------------------------------- #
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_LATIN_WORD_RE = re.compile(r"[A-Za-z']+")
#: Everyday English function words. An Uzbek sentence contains none of them;
#: an English answer is full of them. Names and technical terms are not here,
#: so "iPhone", "Telegram" or "API" never fail the check.
_ENGLISH_WORDS = frozenset(
    """
    the and you your are is was were for with that this have has not sorry
    please can will what how but they their from about into would could should
    dont don't its it's of to in on at as be there here when where which who
    because while also just only really maybe everything something nothing
    help need want know think say said get got let make made time day good
    morning evening night thanks thank

    # Openers the model likes to slip in before switching to Uzbek.
    okay ok sure great alright hello hi hey bye yeah yep nope well
    """.split()
)

#: Function words of the other languages the model has answered in. Only
#: words of four or more letters are kept: short ones like "un" or "et" are
#: real Uzbek words (flour, meat) and would silence a correct sentence.
_OTHER_LANGUAGE_WORDS = frozenset(
    word
    for word in """
    ciao senza cosa hai voglia raccontare ascolto sono molto bene grazie anche
    questo questa quando perche
    hola gracias para como esta estas muy bien pero cuando donde porque tambien
    nada todo ahora luego
    bonjour merci oui elle nous vous avec pour dans mais tres
    hallo danke bitte nicht und ist eine auch sehr
    """.split()
    if len(word) >= 4
)

_FOREIGN_WORDS = _ENGLISH_WORDS | _OTHER_LANGUAGE_WORDS

#: Above these ratios a reply is treated as "not Uzbek". They are deliberately
#: high so a single borrowed name or brand inside an Uzbek sentence still passes.
CYRILLIC_LIMIT = 0.35
FOREIGN_WORD_LIMIT = 0.35
#: A reply mostly written in Devanagari, Arabic, CJK, Hangul or another
#: non-Latin script is not Uzbek. Hindi in particular slipped through before:
#: it has no Cyrillic and no English words, so both older checks passed it.
OTHER_SCRIPT_LIMIT = 0.2

#: Unicode blocks no Uzbek reply should be written in (Cyrillic has its own
#: ratio above, and Latin is the expected script).
_OTHER_SCRIPT_RANGES = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0700, 0x074F),  # Syriac
    (0x0900, 0x097F),  # Devanagari (Hindi, Marathi, Nepali)
    (0x0980, 0x09FF),  # Bengali
    (0x0A00, 0x0A7F),  # Gurmukhi
    (0x0A80, 0x0AFF),  # Gujarati
    (0x0B00, 0x0B7F),  # Oriya
    (0x0B80, 0x0BFF),  # Tamil
    (0x0C00, 0x0C7F),  # Telugu
    (0x0C80, 0x0CFF),  # Kannada
    (0x0D00, 0x0D7F),  # Malayalam
    (0x0E00, 0x0E7F),  # Thai
    (0x0E80, 0x0EFF),  # Lao
    (0x1000, 0x109F),  # Myanmar
    (0x10A0, 0x10FF),  # Georgian
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3040, 0x30FF),  # Hiragana / Katakana
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility
)


def _is_other_script(character: str) -> bool:
    """True for a letter of a script an Uzbek answer is never written in."""
    code = ord(character)
    return any(start <= code <= end for start, end in _OTHER_SCRIPT_RANGES)


@dataclass(frozen=True)
class LanguageReport:
    """Why a reply was or was not accepted as Uzbek."""

    ok: bool
    reasons: tuple[str, ...] = ()
    cyrillic_ratio: float = 0.0
    foreign_word_ratio: float = 0.0
    letters: int = 0
    other_script_ratio: float = 0.0

    @property
    def detail(self) -> str:
        return ", ".join(self.reasons) or "uzbek"


def language_report(text: str) -> LanguageReport:
    """Classify a reply as Uzbek or not.

    This is a heuristic on the text itself: it counts Cyrillic letters,
    everyday English words and letters of other scripts (Devanagari, Arabic,
    CJK, Hangul, ...) against everything else. Text with no letters at all
    (digits, punctuation) is accepted, because there is nothing to translate
    and normalisation has already spelled the numbers out.
    """
    body = text or ""
    letters = _LETTER_RE.findall(body)
    if not letters:
        return LanguageReport(True, (), 0.0, 0.0, 0, 0.0)

    cyrillic_ratio = len(_CYRILLIC_RE.findall(body)) / len(letters)
    words = [w.strip("'").lower() for w in _LATIN_WORD_RE.findall(body)]
    foreign_words = sum(1 for word in words if word in _FOREIGN_WORDS)
    foreign_ratio = foreign_words / len(words) if words else 0.0
    other_script = sum(1 for letter in letters if _is_other_script(letter))
    other_ratio = other_script / len(letters)
    reasons: list[str] = []
    if cyrillic_ratio > CYRILLIC_LIMIT:
        reasons.append("cyrillic")
    if other_script >= 2 and other_ratio > OTHER_SCRIPT_LIMIT:
        reasons.append("script")
    # Two foreign words are required: a single "ok" inside an otherwise Uzbek
    # sentence must not silence the whole answer.
    if foreign_ratio > FOREIGN_WORD_LIMIT and foreign_words >= 2:
        reasons.append("foreign")
    return LanguageReport(
        ok=not reasons,
        reasons=tuple(reasons),
        cyrillic_ratio=round(cyrillic_ratio, 3),
        foreign_word_ratio=round(foreign_ratio, 3),
        letters=len(letters),
        other_script_ratio=round(other_ratio, 3),
    )


def is_uzbek(text: str) -> bool:
    """True when ``text`` can safely be sent to the Uzbek voice."""
    return language_report(text).ok


#: A foreign filler at the very start of a sentence ("Okay,", "Ciao,",
#: "Well,"). It is removed instead of dropping the sentence: the rest of the
#: sentence is usually good Uzbek and should still be spoken.
_FOREIGN_OPENER_RE = re.compile(
    r"^\s*(?:okay|ok|sure|great|alright|certainly|of course|well|cool|fine|"
    r"hello|hi|hey|ciao|hola|bonjour|hallo|guten tag)\b[\s,!.\u2026-]*",
    re.IGNORECASE,
)


def strip_foreign_openers(text: str) -> str:
    """Remove leading foreign filler words, keeping the Uzbek rest.

    Returns an empty string when the sentence was nothing but filler, which
    the caller treats as "do not speak this".
    """
    result = (text or "").strip()
    while result:
        cleaned = _FOREIGN_OPENER_RE.sub("", result, count=1).strip()
        if cleaned == result:
            break
        result = cleaned
    return result


def voice_matches_language(voice: str, language: str) -> bool:
    """True when an Edge TTS voice name belongs to ``language`` ("uz-UZ-...", "uz")."""
    locale = (voice or "").split("-")[0].strip().lower()
    return bool(locale) and locale == (language or "").strip().lower()
