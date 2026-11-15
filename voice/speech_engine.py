"""OpenAI speech engine.

Transcription uses the audio transcriptions endpoint and synthesis uses the
speech endpoint. Both are real calls with bounded retries; the engine reports
"not configured" rather than producing placeholder audio when no key is set.
"""

from __future__ import annotations

import logging
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from config import Settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3


class VoiceError(Exception):
    """Speech operation failed."""

    status_code = 502

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class VoiceNotConfigured(VoiceError):
    status_code = 503


class SpeechEngine:
    """Thin, mockable wrapper around OpenAI audio endpoints."""

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def configured(self) -> bool:
        return self._client is not None or self._settings.openai_configured

    def _ensure_client(self) -> AsyncOpenAI:
        if self._client is None:
            key = self._settings.openai_key
            if not key:
                raise VoiceNotConfigured(
                    "OpenAI API key is not configured; speech is unavailable."
                )
            self._client = AsyncOpenAI(
                api_key=key,
                timeout=self._settings.openai_timeout_seconds,
                max_retries=0,
            )
        return self._client

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "utterance.webm",
        language: str | None = None,
    ) -> str:
        """Return the transcript, or an empty string for silence/no speech."""
        if not audio:
            return ""
        client = self._ensure_client()

        kwargs: dict[str, Any] = {
            "model": self._settings.openai_stt_model,
            "file": (filename, audio),
        }
        if language:
            kwargs["language"] = language

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.audio.transcriptions.create(**kwargs)
                text = getattr(response, "text", "") or ""
                return str(text).strip()
            except (APITimeoutError, APIConnectionError, APIStatusError) as exc:
                last_error = exc
                logger.warning(
                    "Transcription attempt %d/%d failed: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                if isinstance(exc, APIStatusError) and exc.status_code < 500:
                    break
            except Exception as exc:  # noqa: BLE001 - SDK surface can change
                last_error = exc
                logger.exception("Transcription failed")
                break

        raise VoiceError(
            "Speech recognition failed. Please try again."
        ) from last_error

    async def synthesize(self, text: str) -> bytes:
        """Return MP3 bytes for ``text``."""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("text must not be empty")
        client = self._ensure_client()

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.audio.speech.create(
                    model=self._settings.openai_tts_model,
                    voice=self._settings.openai_tts_voice,
                    input=cleaned[:4000],
                    response_format="mp3",
                )
                data = await self._read_binary(response)
                if not data:
                    raise VoiceError("Speech synthesis returned no audio.")
                return data
            except (APITimeoutError, APIConnectionError, APIStatusError) as exc:
                last_error = exc
                logger.warning(
                    "Speech synthesis attempt %d/%d failed: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                if isinstance(exc, APIStatusError) and exc.status_code < 500:
                    break
            except VoiceError:
                raise
            except Exception as exc:  # noqa: BLE001 - SDK surface can change
                last_error = exc
                logger.exception("Speech synthesis failed")
                break

        raise VoiceError("Speech synthesis failed. Please try again.") from last_error

    @staticmethod
    async def _read_binary(response: Any) -> bytes:
        """Read bytes from the SDK response across sync/async wrappers."""
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        aread = getattr(response, "aread", None)
        if aread is not None:
            data = aread()
            if hasattr(data, "__await__"):
                data = await data
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        read = getattr(response, "read", None)
        if read is not None:
            data = read()
            if hasattr(data, "__await__"):
                data = await data
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        return b""
