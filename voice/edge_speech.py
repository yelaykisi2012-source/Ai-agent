"""Keyless text-to-speech through the Microsoft Edge neural voices.

The ``edge-tts`` package speaks the same public endpoint the Edge browser uses
for "Read aloud". There is no account and no API key, which makes it a good
fallback when a quota-limited provider (Gemini TTS) answers HTTP 429.

Output contract matches the rest of the project: ``synthesize(..., "pcm")``
returns raw 24 kHz mono signed-16-bit little-endian samples, which the Telegram
call path upsamples to 48 kHz. MP3 is only a wire format here.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any

from anyio import to_thread

from config import Settings
from voice.audio import ffmpeg_path
from voice.speech_engine import VoiceError, VoiceNotConfigured

logger = logging.getLogger(__name__)

#: Telegram's call path expects 24 kHz mono int16 before upsampling.
TTS_SAMPLE_RATE = 24_000
_FFMPEG_TIMEOUT_SECONDS = 60
_MAX_TEXT_LENGTH = 4000
#: Edge occasionally closes the stream without audio (``NoAudioReceived``);
#: a short retry fixes it, more attempts only delay the call.
_MAX_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 0.3

try:  # optional dependency: the app still runs without it
    import edge_tts as _edge_tts
except Exception:  # noqa: BLE001 - a broken install must not stop the app
    _edge_tts = None


class EdgeSpeechEngine:
    """Synthesis-only engine backed by the keyless Edge neural voices."""

    name = "edge"

    def __init__(self, settings: Settings, engine: Any | None = None) -> None:
        self._settings = settings
        self._edge = engine if engine is not None else _edge_tts

    @property
    def configured(self) -> bool:
        return self._edge is not None

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "utterance.webm",
        language: str | None = None,
    ) -> str:
        raise VoiceNotConfigured(
            "Edge TTS only synthesises speech; set SPEECH_PROVIDER for transcription."
        )

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("text must not be empty")
        if self._edge is None:
            raise VoiceNotConfigured(
                "Edge TTS is not available: install the 'edge-tts' package."
            )

        voice = self._settings.edge_tts_voice
        audio = bytearray()
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            audio.clear()
            try:
                communicate = self._communicate(cleaned[:_MAX_TEXT_LENGTH], voice)
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio":
                        audio.extend(chunk.get("data") or b"")
            except Exception as exc:  # noqa: BLE001 - network/protocol errors vary
                # Log the type only: URLs and headers can carry tokens.
                last_error = exc
                logger.warning(
                    "Edge TTS attempt %d/%d failed: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                )
            else:
                if audio:
                    break
                last_error = VoiceError("Edge speech synthesis returned no audio.")
                logger.warning(
                    "Edge TTS attempt %d/%d returned no audio", attempt, _MAX_ATTEMPTS
                )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)

        if not audio:
            raise VoiceError("Edge speech synthesis failed.") from last_error
        data = bytes(audio)
        logger.info("Edge TTS: %d bytes of MP3 for %d characters", len(data), len(cleaned))
        if response_format == "mp3":
            return data
        return await to_thread.run_sync(_mp3_to_pcm, data)

    def _communicate(self, text: str, voice: str) -> Any:
        """Build a Communicate request with the configured prosody.

        Older ``edge-tts`` releases (and test doubles) do not accept the
        prosody arguments; falling back to the defaults keeps that sentence
        alive instead of failing the whole call turn.
        """
        try:
            return self._edge.Communicate(
                text,
                voice,
                rate=self._settings.edge_tts_rate,
                pitch=self._settings.edge_tts_pitch,
                volume=self._settings.edge_tts_volume,
            )
        except TypeError:
            logger.debug("Edge TTS: prosody options unsupported; using the defaults")
            return self._edge.Communicate(text, voice)


def _mp3_to_pcm(audio: bytes) -> bytes:
    """Convert MP3 to 24 kHz mono int16 PCM with FFmpeg."""
    if not audio:
        raise VoiceError("Cannot convert empty audio.")
    executable = ffmpeg_path()
    if executable is None:
        raise VoiceError("FFmpeg is not installed; cannot convert TTS audio.")
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(TTS_SAMPLE_RATE),
        "-ac",
        "1",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            input=audio,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VoiceError("FFmpeg could not convert TTS audio.") from exc
    if completed.returncode != 0 or not completed.stdout:
        logger.warning("FFmpeg TTS conversion failed (rc=%s)", completed.returncode)
        raise VoiceError("FFmpeg could not convert TTS audio.")
    pcm = completed.stdout
    if len(pcm) % 2:  # int16 samples, never return a half sample
        pcm = pcm[:-1]
    return pcm
