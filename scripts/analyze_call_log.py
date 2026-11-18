"""Summarise real Telegram call latency from ``logs/agent.log``.

Read-only: it parses the log the app already writes and prints, per call and
per utterance, the STT / LLM / TTS durations, queue wait, time to first audio
and the pipeline total. Barge-in, stale and superseded counts are reported too.

Usage:
    ./.venv/Scripts/python.exe scripts/analyze_call_log.py [--file logs/agent.log]
                                                           [--after "2026-11-16 14:40"]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
UTTERANCE = re.compile(r"utterance #(\d+): (.+)$")
DURATION = re.compile(r"in (\d+\.\d+)s")
CHARS = re.compile(r"\((\d+) chars\)")


@dataclass
class Utterance:
    index: int
    stt: float | None = None
    stt_chars: int | None = None
    stt_error: str | None = None
    llm: float | None = None
    llm_chars: int | None = None
    llm_empty: bool = False
    tts: float | None = None
    tts_frames: int | None = None
    tts_error: str | None = None
    first_audio: float | None = None
    queue_wait: float | None = None
    total: float | None = None


@dataclass
class Session:
    started: str = ""
    direction: str = ""
    ended: str = ""
    summary: str = ""
    utterances: dict[int, Utterance] = field(default_factory=dict)
    barge_ins: int = 0
    stale: int = 0
    superseded: int = 0
    dropped: int = 0
    empty_replies: int = 0


def parse(path: Path, after: str | None) -> list[Session]:
    sessions: list[Session] = []
    current: Session | None = None

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stamp_match = STAMP.match(line)
        if not stamp_match:
            continue
        stamp = f"{stamp_match.group(1)}.{stamp_match.group(2)}"
        if after and stamp[: len(after)] < after:
            continue

        if "Telegram call: audio path active" in line:
            current = Session(started=stamp)
            direction = re.search(r"direction=(\w+)", line)
            current.direction = direction.group(1) if direction else "?"
            sessions.append(current)
            continue
        if current is None:
            continue

        if "Telegram call: ended (" in line:
            current.ended = stamp
            current.summary = line.split("Telegram call: ended (", 1)[1].rstrip(")")
            current = None
            continue

        if "barge-in accepted" in line:
            current.barge_ins += 1
            continue
        if "skipping stale utterance" in line:
            current.stale += 1
            continue
        if "superseded by a newer one" in line:
            current.superseded += 1
            continue
        if "backlog full" in line:
            current.dropped += 1
            continue

        match = UTTERANCE.search(line)
        if not match:
            continue
        index = int(match.group(1))
        event = match.group(2)
        item = current.utterances.setdefault(index, Utterance(index=index))

        if event.startswith("STT done"):
            duration = DURATION.search(event)
            chars = CHARS.search(event)
            item.stt = float(duration.group(1)) if duration else None
            item.stt_chars = int(chars.group(1)) if chars else None
        elif event.startswith("STT failed"):
            item.stt_error = event.rsplit("(", 1)[-1].rstrip(")") if "(" in event else "?"
        elif event.startswith("LLM done"):
            duration = DURATION.search(event)
            chars = CHARS.search(event)
            item.llm = float(duration.group(1)) if duration else None
            item.llm_chars = int(chars.group(1)) if chars else None
        elif "LLM returned no text" in event or event.startswith("LLM failed"):
            item.llm_empty = "no text" in event
            item.llm = 0.0 if item.llm_empty else item.llm
            current.empty_replies += 1
        elif event.startswith("TTS done"):
            duration = DURATION.search(event)
            frames = re.search(r"\((\d+) frames\)", event)
            item.tts = float(duration.group(1)) if duration else None
            item.tts_frames = int(frames.group(1)) if frames else None
        elif event.startswith("TTS failed"):
            item.tts_error = event.rsplit("(", 1)[-1].rstrip(")") if "(" in event else "?"

        if "first audio queued" in line:
            first = re.search(r"first audio queued (\d+\.\d+)s", line)
            if first:
                item.first_audio = float(first.group(1))
        if "pipeline finished" in line:
            total = re.search(r"finished in (\d+\.\d+)s", line)
            wait = re.search(r"queue wait (\d+\.\d+)s", line)
            item.total = float(total.group(1)) if total else None
            item.queue_wait = float(wait.group(1)) if wait else None
    return sessions


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def print_sessions(sessions: list[Session]) -> None:
    if not sessions:
        print("No complete call session found for the requested window.")
        return
    for number, session in enumerate(sessions, 1):
        print(f"\n=== Call {number}: {session.started} ({session.direction}) ===")
        if session.summary:
            print(f"end: {session.ended} | {session.summary}")
        print(
            "barge-ins:", session.barge_ins,
            "| stale:", session.stale,
            "| superseded:", session.superseded,
            "| backlog drops:", session.dropped,
            "| empty replies:", session.empty_replies,
        )
        print("| # | queue | STT | LLM | TTS | first audio | total | note |")
        print("|---|-------|-----|-----|-----|-------------|-------|------|")
        for index in sorted(session.utterances):
            item = session.utterances[index]
            note = []
            if item.stt_error:
                note.append(f"stt:{item.stt_error}")
            if item.llm_empty:
                note.append("llm:empty")
            if item.tts_error:
                note.append(f"tts:{item.tts_error}")
            if item.llm_chars:
                note.append(f"reply={item.llm_chars}ch")
            if item.stt_chars:
                note.append(f"heard={item.stt_chars}ch")
            print(
                f"| {index} | {_fmt(item.queue_wait)} | {_fmt(item.stt)} | "
                f"{_fmt(item.llm)} | {_fmt(item.tts)} | {_fmt(item.first_audio)} | "
                f"{_fmt(item.total)} | {'; '.join(note) or '-'} |"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="logs/agent.log")
    parser.add_argument("--after", default=None, help='only lines after "YYYY-MM-DD HH:MM"')
    args = parser.parse_args()
    print_sessions(parse(Path(args.file), args.after))


if __name__ == "__main__":
    main()
