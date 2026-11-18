"""Live checks for the Uzbek voice call and the automatic goodbye.

The automated suite (``tests/test_call_ending.py``, ``tests/test_guest_chat.py``,
``tests/test_uzbek_voice.py``) proves the logic with fakes. It cannot prove what
a real Telegram call sounds like, so this script does the part that needs the
network, the owner's account and a real phone:

    # 1. configuration and voice checks only (no network, nothing is sent)
    .\\.venv\\Scripts\\python.exe scripts/live_call_check.py

    # 2. synthesize a sample line to a WAV file you can listen to
    .\\.venv\\Scripts\\python.exe scripts/live_call_check.py --sample data/sample.wav

    # 3. connect the real Telegram account and report what it sees
    .\\.venv\\Scripts\\python.exe scripts/live_call_check.py --connect

    # 4. place a real call to the owner, then read the log for evidence
    .\\.venv\\Scripts\\python.exe scripts/live_call_check.py --call

    # 5. only read the log of a call you already made
    .\\.venv\\Scripts\\python.exe scripts/live_call_check.py --report

Nothing here claims a result it did not observe: every line printed is either a
value read from the configuration, a file that was written, or a log line that
was actually found.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, get_settings  # noqa: E402
from telegram.audio import pcm16_to_wav  # noqa: E402
from voice.uzbek import (  # noqa: E402
    prepare_for_tts,
    unread_digits,
    voice_matches_language,
)

#: The line used for the listening sample: dates, times, money, percent and a
#: phone number, so one file shows every number rule at once.
SAMPLE_TEXT = (
    "Bugun 16-noyabr, soat 12:30. Hisobda 1 500 so‘m bor, batareya 25%, "
    "telefonim +998 90 123 45 67."
)

#: Log lines that prove the new behaviour ran during a real call.
EVIDENCE = (
    ("farewell detected", "a closing phrase was recognised in a real call"),
    ("saying goodbye before hanging up", "Ksenus answered with a goodbye"),
    ("nothing more was said after the goodbye", "the grace window passed, the call ended"),
    ("goodbye cancelled", "new speech cancelled the ending"),
    ("reply trimmed for speech", "a long answer was shortened for speech"),
    ("Voice reply was not Uzbek", "the language guard caught a foreign answer"),
    ("Rewrite is still not Uzbek", "the guard stayed silent instead of speaking it"),
    ("agreed to be called", "a guest gave consent to a call"),
    ("Telegram call: refused", "a call was refused"),
)

#: Log lines that must never appear. These are real problems, not guard work.
PROBLEMS = (
    ("digit(s) reach TTS unconverted", "a number reached TTS as digits"),
    ("nothing in this reply was Uzbek", "a reply had no Uzbek sentence at all"),
    ("Traceback (most recent call last)", "an exception reached the log"),
)


def _line(ok: bool | None, label: str, detail: str = "") -> None:
    mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


def check_settings(settings: Settings) -> bool:
    """Print what is configured. Nothing is sent anywhere."""
    print("== Configuration ==")
    _line(settings.telegram_enabled, "TELEGRAM_ENABLED")
    _line(settings.telegram_configured, "Telegram API credentials present")
    _line(
        settings.telegram_owner_user_id is not None,
        "TELEGRAM_OWNER_USER_ID",
        str(settings.telegram_owner_user_id or "not set"),
    )
    _line(settings.telegram_calls_enabled, "TELEGRAM_CALLS_ENABLED")
    _line(
        settings.telegram_public_chat_enabled,
        "TELEGRAM_PUBLIC_CHAT_ENABLED",
        "any user may chat as a guest"
        if settings.telegram_public_chat_enabled
        else "only the owner and TELEGRAM_ALLOWED_USER_IDS",
    )
    _line(
        None,
        "TELEGRAM_ALLOWED_USER_IDS",
        str(settings.telegram_allowed_user_ids or "empty"),
    )
    _line(
        None,
        "TELEGRAM_CALL_CONSENT_SECONDS",
        f"{settings.telegram_call_consent_seconds} s for one yes",
    )
    _line(
        None,
        "TELEGRAM_CALL_GOODBYE_GRACE_SECONDS",
        f"{settings.telegram_call_goodbye_grace_seconds} s of silence after the goodbye",
    )
    _line(None, "VOICE_LANGUAGE", settings.voice_language)
    _line(
        None,
        "STT_LANGUAGE",
        settings.stt_language or "(empty: the provider detects the language)",
    )
    return settings.telegram_configured and settings.telegram_calls_enabled


def check_voice(settings: Settings) -> None:
    """The configured TTS voice really is an Uzbek voice."""
    print("\n== TTS voice ==")
    voice = settings.edge_tts_voice
    ok = voice_matches_language(voice, settings.voice_language)
    _line(
        ok,
        f"EDGE_TTS_VOICE={voice}",
        "matches the spoken language"
        if ok
        else f"will not pronounce {settings.voice_language} correctly",
    )


def write_sample(settings: Settings, path: Path) -> None:
    """Synthesize the sample line through the real TTS and save it as WAV."""
    print("\n== Listening sample ==")
    from voice.speech_engine import build_speech_engine

    spoken = prepare_for_tts(SAMPLE_TEXT)
    print(f"prepared text: {spoken}")
    left = unread_digits(spoken)
    _line(not left, "no digits left for TTS to read as symbols", str(left or ""))

    engine = build_speech_engine(settings)
    _line(engine.configured, "speech engine configured")
    if not engine.configured:
        return

    pcm = asyncio.run(engine.synthesize(spoken, response_format="pcm"))
    _line(bool(pcm), "TTS returned audio", f"{len(pcm)} bytes of 24 kHz PCM")
    if not pcm:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm16_to_wav(pcm, sample_rate=24000))
    print(f"written: {path.resolve()}")
    print("Listen for: o‘n besh / yigirma besh foiz / bir ming besh yuz so‘m /")
    print("          soat o‘n ikki yarim / o‘n oltinchi noyabr /")
    print("          the phone number read digit by digit.")


async def connect(settings: Settings) -> None:
    """Connect the real account, report the state, disconnect again."""
    print("\n== Telegram account ==")
    from telegram.account import TelegramAccount

    account = TelegramAccount(settings)
    detail = await account.start()
    _line(account.ready, "connected and authorized", detail)
    if account.ready:
        print(f"       own id: {account.self_id}")
    await account.stop()


async def call(settings: Settings) -> None:
    """Place one real call through the production runtime, then stop."""
    print("\n== Real call ==")
    if not settings.telegram_calls_enabled:
        _line(False, "TELEGRAM_CALLS_ENABLED is false; refusing to dial")
        return
    if settings.telegram_owner_user_id is None:
        _line(False, "TELEGRAM_OWNER_USER_ID is not set; refusing to dial")
        return

    from runtime import AgentRuntime

    runtime = AgentRuntime(settings)
    await runtime.start()
    _line(runtime.telegram.ready, "Telegram ready")
    if not runtime.telegram.ready:
        await runtime.stop()
        return

    print("Say, in this order:")
    print("  1. a normal question, and listen for an Uzbek answer;")
    print("  2. 'xayr' or 'ko‘rishguncha';")
    print("  3. nothing — Ksenus says goodbye and should hang up by itself;")
    print("  4. call again and say 'xayr', then keep talking: it must NOT hang up.")
    started, detail = await runtime.calls.place("live_call_check")
    _line(started, "call placed", detail)
    if started:
        while runtime.calls.active:
            await asyncio.sleep(1)
        print("the call is over")
    await runtime.stop()


def report(settings: Settings, tail: int = 4000) -> None:
    """Read the log of a real call and print only what is actually there."""
    print("\n== Log evidence ==")
    log = Path(settings.log_dir) / "agent.log"
    if not log.is_file():
        _line(False, f"{log} not found")
        return
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]

    print(f"reading the last {len(lines)} line(s) of {log}")
    print(
        "note: the test suite writes to the same log, so failure paths that were\n"
        "      only simulated in tests can appear here too. Compare timestamps\n"
        "      with the call you actually made."
    )
    for marker, meaning in EVIDENCE:
        hits = [line.strip() for line in lines if marker in line]
        _line(bool(hits), meaning, f"{len(hits)} line(s)")
        for hit in hits[-2:]:
            print(f"       {hit[:200]}")

    print("\n-- must stay at zero --")
    total = 0
    for marker, meaning in PROBLEMS:
        hits = [line.strip() for line in lines if marker in line]
        total += len(hits)
        _line(not hits, meaning, f"{len(hits)} line(s)")
        for hit in hits[-2:]:
            print(f"       {hit[:200]}")
    if total:
        print(f"\n{total} problem line(s) found; the checks above are not clean.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sample", type=Path, help="write a WAV listening sample here")
    parser.add_argument("--connect", action="store_true", help="connect the real account")
    parser.add_argument("--call", action="store_true", help="place one real call")
    parser.add_argument("--report", action="store_true", help="read logs/agent.log")
    args = parser.parse_args()

    settings = get_settings()
    usable = check_settings(settings)
    check_voice(settings)

    if args.sample is not None:
        write_sample(settings, args.sample)
    if args.connect:
        asyncio.run(connect(settings))
    if args.call:
        asyncio.run(call(settings))
    if args.report or args.call:
        report(settings)

    if not (args.sample or args.connect or args.call or args.report):
        print("\nNothing was sent. Use --sample, --connect, --call or --report.")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
