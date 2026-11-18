"""Privacy filter for replies that leave the house.

The owner's assistant talks to the owner, and from now on it can also talk to
other Telegram users. A guest turn already runs without tools, so it cannot
read the owner's data on purpose; this module covers the other case, where the
model repeats something it saw in its own context: a key, a path, a phone
number or an internal setting.

Redaction is a last line of defence, not the first one. It only replaces text
that is shaped like a secret; it does not judge the content of an answer.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: What replaces anything that was removed. One word, in Uzbek, so a guest sees
#: that something was withheld instead of a gap they can guess about.
REDACTED = "[yashirin]"

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"\b(?:sk|pk|ghp|xox[baprs]|AIza)[A-Za-z0-9._\-]{12,}")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    (
        "env_assignment",
        re.compile(r"\b[A-Z][A-Z0-9_]{3,}(?:_KEY|_TOKEN|_HASH|_SECRET|_PASSWORD|_ID)\s*=\s*\S+"),
    ),
    ("windows_path", re.compile(r"\b[A-Za-z]:[\\/][^\s'\"]{2,}")),
    # No \b before the slash: " /home/user" has no word boundary there, and a
    # lookbehind still keeps a path fragment inside a longer path from matching.
    ("unix_path", re.compile(r"(?<![\w/])/(?:home|root|Users|etc|var|opt)/[^\s'\"]{2,}")),
    ("long_token", re.compile(r"\b(?=[A-Za-z0-9._\-]{32,}\b)(?:[A-Za-z0-9._\-]*[A-Z])(?:[A-Za-z0-9._\-]*\d)[A-Za-z0-9._\-]*")),
)


_PATTERNS_ONLY = tuple(pattern for _name, pattern in _SECRET_PATTERNS)

#: A path may contain spaces ("F:\Vs code\Ai agent\data\agent.db"), and a
#: token-shaped match would stop at the first space and leave the rest of the
#: path in the reply. For these two patterns the removal is extended to the end
#: of the sentence: losing a few extra words in a guest reply costs far less
#: than leaking the tail of a location.
_PATH_NAMES = frozenset({"windows_path", "unix_path"})
_SENTENCE_END = re.compile(r"[.!?\n]")


def _extend_to_sentence_end(text: str, end: int) -> int:
    """The index of the sentence terminator after ``end`` (or the text length)."""
    match = _SENTENCE_END.search(text, end)
    return match.start() if match is not None else len(text)


def _substitute(pattern: re.Pattern[str], text: str, *, extend: bool = False) -> str:
    """Replace every match, optionally up to the end of its sentence."""
    if not extend:
        return pattern.sub(REDACTED, text)
    parts: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        stop = max(_extend_to_sentence_end(text, match.end()), match.end())
        parts.append(text[cursor:match.start()])
        parts.append(REDACTED)
        cursor = stop
    parts.append(text[cursor:])
    return "".join(parts)


def redact(text: str, *, extra_secrets: tuple[str, ...] = ()) -> tuple[str, bool]:
    """Return ``(text, changed)`` with anything secret-shaped replaced.

    ``extra_secrets`` are literal strings that must never leave the process for
    this recipient, typically the owner's phone number and numeric user id.
    Short or empty values are ignored so an ordinary word cannot be masked.
    """
    if not text:
        return text, False
    result = text
    for name, pattern in _SECRET_PATTERNS:
        result = _substitute(pattern, result, extend=name in _PATH_NAMES)
    for secret in extra_secrets:
        value = (secret or "").strip()
        if len(value) >= 5 and value in result:
            result = result.replace(value, REDACTED)
    changed = result != text
    if changed:
        # No content is logged: only the fact that something was withheld.
        logger.warning("A guest reply contained secret-shaped text and was redacted")
    return result, changed


def looks_secret(text: str) -> bool:
    """True when ``text`` contains anything secret-shaped (no changes made)."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PATTERNS_ONLY)
