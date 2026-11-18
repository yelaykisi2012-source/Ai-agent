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
                base_url=self._settings.openai_base_url,
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

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        """Return audio bytes for ``text`` in the requested format.

        ``response_format="pcm"`` returns raw 24 kHz mono int16 little-endian
        samples, which the Telegram call path upsamples to 48 kHz itself.
        """
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
                    response_format=response_format,
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


class ComposedSpeechEngine:
    """Transcription from one backend, speech synthesis from another.

    ``transcribe`` always uses the speech provider (Gemini/OpenAI). Synthesis
    uses the configured TTS backend and falls back to a second provider when
    the primary fails, so a Gemini TTS HTTP 429 still produces audio.
    """

    def __init__(
        self,
        stt: Any,
        tts: Any,
        fallback: Any | None = None,
        *,
        stt_name: str = "",
        tts_name: str = "",
        fallback_name: str = "",
    ) -> None:
        self._stt = stt
        self._tts = tts
        self._fallback = fallback
        self._stt_name = stt_name
        self._tts_name = tts_name
        self._fallback_name = fallback_name

    @property
    def configured(self) -> bool:
        # The call path needs transcription first; a TTS-only fallback must not
        # make an unconfigured engine look ready.
        return bool(getattr(self._stt, "configured", False))

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "utterance.webm",
        language: str | None = None,
    ) -> str:
        return await self._stt.transcribe(audio, filename=filename, language=language)

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        if self._tts.configured:
            try:
                return await self._tts.synthesize(
                    text, response_format=response_format
                )
            except VoiceError as exc:
                if self._fallback is None:
                    raise
                # Provider message and type only: no prompts, no keys.
                logger.warning(
                    "TTS primary (%s) failed (%s); trying fallback (%s)",
                    self._tts_name,
                    type(exc).__name__,
                    self._fallback_name,
                )
        if self._fallback is None:
            # Not configured: let the primary raise its diagnostic error.
            return await self._tts.synthesize(text, response_format=response_format)

        data = await self._fallback.synthesize(text, response_format=response_format)
        logger.info(
            "TTS fallback (%s) produced %d bytes (%s)",
            self._fallback_name,
            len(data),
            response_format,
        )
        return data


def _build_engine(provider: str, settings: Settings) -> Any:
    if provider == "gemini":
        from voice.gemini_speech import GeminiSpeechEngine

        return GeminiSpeechEngine(settings)
    if provider == "edge":
        from voice.edge_speech import EdgeSpeechEngine

        return EdgeSpeechEngine(settings)
    return SpeechEngine(settings)


def _check_tts_language(settings: Settings, tts_provider: str) -> None:
    """Say out loud when the chosen voice is not a voice for the spoken language.

    Edge voice names carry their locale ("uz-UZ-MadinaNeural"). A voice from
    another locale still returns audio, so nothing would fail visibly, but it
    will not pronounce Uzbek correctly. Reporting that is the honest option;
    treating any voice as Uzbek would hide a real configuration problem.
    """
    if tts_provider != "edge":
        return
    from voice.uzbek import voice_matches_language

    if voice_matches_language(settings.edge_tts_voice, settings.voice_language):
        logger.info(
            "TTS voice %s is a %s voice", settings.edge_tts_voice, settings.voice_language
        )
        return
    logger.warning(
        "EDGE_TTS_VOICE=%s is not a %s voice: it will not pronounce Uzbek "
        "correctly. Set a %s-* voice such as uz-UZ-MadinaNeural or "
        "uz-UZ-SardorNeural.",
        settings.edge_tts_voice,
        settings.voice_language,
        settings.voice_language,
    )


def build_speech_engine(settings: Settings) -> Any:
    """Return the configured speech backend.

    ``SPEECH_PROVIDER`` selects transcription (openai or gemini), ``TTS_PROVIDER``
    may override synthesis (defaults to the speech provider), and
    ``TTS_FALLBACK_PROVIDER`` adds a second synthesis backend used only when the
    primary fails (for example Gemini TTS HTTP 429).
    """
    stt_provider = settings.speech_provider
    tts_provider = settings.tts_backend
    _check_tts_language(settings, tts_provider)
    stt = _build_engine(stt_provider, settings)
    tts = stt if tts_provider == stt_provider else _build_engine(tts_provider, settings)

    fallback_provider = settings.tts_fallback_provider
    fallback: Any | None = None
    if fallback_provider and fallback_provider != tts_provider:
        candidate = _build_engine(fallback_provider, settings)
        if getattr(candidate, "configured", False):
            fallback = candidate
        else:
            logger.warning(
                "TTS fallback %r is not configured; primary TTS only",
                fallback_provider,
            )
        if fallback is not None:
            _check_tts_language(settings, fallback_provider)

    if fallback is None and tts is stt:
        return stt
    return ComposedSpeechEngine(
        stt,
        tts,
        fallback,
        stt_name=stt_provider,
        tts_name=tts_provider,
        fallback_name=fallback_provider,
    )
