"""Audio helpers backed by FFmpeg.

FFmpeg is used only for format conversion: Speech-to-text accepts most upload
formats directly, but Telegram voice notes must be OGG/Opus, and browsers
record WebM/Opus. Every function fails loudly instead of returning broken
audio.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES: frozenset[str] = frozenset(
    {".webm", ".ogg", ".oga", ".mp3", ".wav", ".m4a", ".mp4", ".mpga", ".mpeg", ".flac"}
)
_FFMPEG_TIMEOUT_SECONDS = 60


class AudioError(Exception):
    """Audio could not be read or converted."""


def ffmpeg_path() -> str | None:
    """Return the ffmpeg executable path, or None when unavailable."""
    return shutil.which("ffmpeg")


def ensure_upload(filename: str | None, data: bytes, max_mb: int) -> str:
    """Validate an uploaded audio blob and return a safe filename."""
    if not data:
        raise AudioError("The audio file is empty.")
    limit = max_mb * 1024 * 1024
    if len(data) > limit:
        raise AudioError(f"The audio file exceeds the {max_mb} MB limit.")
    name = Path(filename or "utterance.webm").name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise AudioError(
            f"Unsupported audio format {suffix or '(none)'}. "
            f"Supported: {', '.join(sorted(ALLOWED_SUFFIXES))}."
        )
    return name


def to_ogg_opus(source: bytes) -> bytes:
    """Convert audio bytes to OGG/Opus suitable for a Telegram voice note."""
    executable = ffmpeg_path()
    if executable is None:
        raise AudioError("FFmpeg is not installed; cannot create a voice note.")
    if not source:
        raise AudioError("Cannot convert empty audio.")

    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-f",
        "ogg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            input=source,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError("FFmpeg conversion failed or timed out.") from exc

    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
        logger.warning("FFmpeg conversion failed: %s", detail)
        raise AudioError("FFmpeg could not convert the audio.")
    return completed.stdout


def duration_seconds(source: bytes) -> float | None:
    """Best-effort duration probe using ffprobe. Returns None when unknown."""
    executable = shutil.which("ffprobe")
    if executable is None or not source:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "pipe:0",
            ],
            input=source,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return float(completed.stdout.decode().strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
