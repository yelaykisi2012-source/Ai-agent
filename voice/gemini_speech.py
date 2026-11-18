"""Gemini speech engine (transcription + text-to-speech).

Google's Generative Language API is not Whisper-compatible, so this engine
speaks the native ``generateContent`` protocol:

* transcription uses a transcribe model and returns ``audioTranscription.text``;
* synthesis uses a TTS model with ``responseModalities=["AUDIO"]`` and returns
  a 24 kHz mono WAV, which the Telegram call path upsamples to 48 kHz.

The Gemini key arrives through ``OPENAI_API_KEY`` (the same secret the rest of
the app uses) and is sent as ``x-goog-api-key``. It is never logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import random
import subprocess
import time
import wave
from collections import OrderedDict
from typing import Any

import httpx
from anyio import to_thread

from config import Settings
from voice.audio import ffmpeg_path
from voice.speech_engine import VoiceError, VoiceNotConfigured

logger = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
#: STT and TTS hit separate quotas, so each stage retries on its own budget.
_MAX_ATTEMPTS = {"stt": 2, "tts": 3}
#: A chat model can transcribe too; used when the dedicated STT model is
#: rate limited and GEMINI_STT_FALLBACK_MODEL is configured.
_STT_FALLBACK_PROMPT = (
    "Transcribe the speech in this audio exactly as spoken. "
    "Return only the transcript text, with no commentary."
)
_BASE_BACKOFF_SECONDS = 0.6
_MAX_BACKOFF_SECONDS = 6.0
#: A provider ``Retry-After`` is honoured, but never waits longer than this.
_MAX_RETRY_AFTER_SECONDS = 20.0
#: Extra waiting before a request when the same stage was just rate limited.
_MAX_COOLDOWN_WAIT_SECONDS = 5.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
#: STT is latency-critical: never wait as long as a synthesis request would.
_MAX_RETRY_AFTER_BY_STAGE = {"stt": 1.5, "tts": _MAX_RETRY_AFTER_SECONDS}
_MAX_COOLDOWN_BY_STAGE = {"stt": 1.5, "tts": _MAX_COOLDOWN_WAIT_SECONDS}
#: Identical audio (STT) or text (TTS) is answered from memory: a repeated
#: utterance must not burn another request from a small free quota.
_CACHE_TTL_SECONDS = 120.0
_CACHE_MAX_ENTRIES = 32
_FFMPEG_TIMEOUT_SECONDS = 60
_PCM_SAMPLE_RATE = 24_000


async def _sleep(seconds: float) -> None:
    """Indirection so tests can observe backoff without waiting."""
    await asyncio.sleep(seconds)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a numeric ``Retry-After`` header; HTTP-dates are ignored."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _fallback_prompt(language: str | None) -> str:
    """Instruction for the chat model used when the STT model is rate limited.

    A configured language is mentioned so the transcript comes back in the
    expected script; without it the model transcribes whatever it hears.
    """
    if not language:
        return _STT_FALLBACK_PROMPT
    return f"{_STT_FALLBACK_PROMPT} The spoken language is {language}."


class GeminiSpeechEngine:
    """Speech engine backed by Gemini transcribe and TTS models."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._blocked_until: dict[str, float] = {}
        #: Per-model cooldown: once a model answers 429, skip it for a while
        #: instead of paying the failed request on every single utterance.
        self._model_blocked_until: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self._settings.openai_key)

    # -- public API -------------------------------------------------------- #
    async def transcribe(
        self,
        audio: bytes,
        filename: str = "utterance.webm",
        language: str | None = None,
    ) -> str:
        """Return the transcript, or an empty string for silence.

        ``language`` is only used for the chat-model fallback at the bottom of
        this method. The dedicated transcribe model is sent the audio and
        nothing else: this API payload has no language field for it, and
        adding one the endpoint ignores would only look like a setting.
        """
        if not audio:
            return ""
        wav = await to_thread.run_sync(self._to_wav, audio)
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": base64.b64encode(wav).decode("ascii"),
                            }
                        }
                    ]
                }
            ]
        }
        model = self._settings.gemini_stt_model
        fallback_model = self._settings.gemini_stt_fallback_model.strip()
        has_fallback = bool(fallback_model) and fallback_model != model
        deadline = time.monotonic() + self._settings.gemini_stt_total_seconds
        fallback_payload = {
            "contents": [
                {
                    "parts": [
                        {"text": _fallback_prompt(language)},
                        *payload["contents"][0]["parts"],
                    ]
                }
            ]
        }
        # A model that 429s once is skipped while its cooldown lasts: waiting on
        # a rate-limited model only delays the answer the person is waiting for.
        cooling = self._model_blocked_until.get(model, 0.0) > time.monotonic()
        if has_fallback and cooling:
            logger.info(
                "Gemini STT: %s is cooling down; using %s directly",
                model,
                fallback_model,
            )
            data = await self._generate(
                fallback_model,
                fallback_payload,
                stage="stt",
                attempts=1,
                deadline=deadline,
            )
            return self._transcript_from(data)
        # With an alternative model configured, the first 429 goes straight to
        # it: retrying a rate-limited model only delays the conversation.
        primary_attempts = 1 if has_fallback else None
        try:
            data = await self._generate(
                model, payload, stage="stt", attempts=primary_attempts, deadline=deadline
            )
        except VoiceError:
            if not has_fallback:
                raise
            if time.monotonic() >= deadline:
                logger.warning(
                    "Gemini STT: budget used up; not trying the fallback model"
                )
                raise
            # Same key, different model: the quota is per model, so this is an
            # existing-configuration alternative and needs no new account.
            logger.warning(
                "Gemini STT: %s is rate limited; trying the fallback model %s",
                model,
                fallback_model,
            )
            data = await self._generate(
                fallback_model,
                fallback_payload,
                stage="stt",
                attempts=1,
                deadline=deadline,
            )
        return self._transcript_from(data)

    async def synthesize(self, text: str, response_format: str = "mp3") -> bytes:
        """Return audio bytes in ``pcm`` (24 kHz), ``wav`` or ``mp3`` format."""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("text must not be empty")

        payload = {
            "contents": [{"parts": [{"text": cleaned[:4000]}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": self._settings.gemini_voice
                        }
                    }
                },
            },
        }
        data = await self._generate(
            self._settings.gemini_tts_model, payload, stage="tts"
        )
        wav = self._audio_from(data)

        if response_format == "wav":
            return wav
        pcm = await to_thread.run_sync(self._wav_to_pcm, wav)
        if response_format == "pcm":
            return pcm
        return await to_thread.run_sync(self._pcm_to_mp3, pcm)

    # -- transport --------------------------------------------------------- #
    async def _generate(
        self,
        model: str,
        payload: dict[str, Any],
        *,
        stage: str,
        attempts: int | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """POST one generateContent request with bounded, stage-aware retries.

        STT and TTS quotas are independent, so each stage keeps its own
        cooldown, attempt budget and cache. A 429 is retried with the provider
        ``Retry-After`` when present, otherwise with exponential backoff and
        jitter; it is never retried forever and never logged with a body.
        """
        label = stage.upper()
        attempts = attempts or _MAX_ATTEMPTS[stage]
        # A stalled request must not hold the call for the shared 60 s timeout.
        request_timeout = (
            self._settings.gemini_stt_timeout_seconds
            if stage == "stt"
            else self._settings.gemini_timeout_seconds
        )
        key = self._settings.openai_key
        if not key:
            raise VoiceNotConfigured(
                "Speech is not configured: no API key is available."
            )

        cache_key = self._cache_key(stage, model, payload)
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.info("Gemini %s: reusing a cached response (no request sent)", label)
            return cached

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._settings.gemini_timeout_seconds)

        try:
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                await self._wait_for_cooldown(stage, deadline)
                try:
                    response = await client.post(
                        f"{BASE_URL}/models/{model}:generateContent",
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json=payload,
                        timeout=request_timeout,
                    )
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt >= attempts:
                        break
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "Gemini %s attempt %d/%d network error (%s); retrying in %.2fs",
                        label,
                        attempt,
                        attempts,
                        type(exc).__name__,
                        delay,
                    )
                    await _sleep(delay)
                    continue

                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise VoiceError("Gemini returned an unreadable response.") from exc
                    if isinstance(data, dict):
                        self._cache_put(cache_key, data)
                    return data

                retry_after = _retry_after_seconds(response)
                if retry_after is not None:
                    retry_after = min(
                        retry_after,
                        _MAX_RETRY_AFTER_BY_STAGE.get(stage, _MAX_RETRY_AFTER_SECONDS),
                    )
                retryable = response.status_code in _RETRYABLE_STATUS
                # Never log the body: provider errors can echo key fragments.
                logger.warning(
                    "Gemini %s attempt %d/%d rejected: HTTP %d (retry_after=%s)",
                    label,
                    attempt,
                    attempts,
                    response.status_code,
                    f"{retry_after:.1f}s" if retry_after is not None else "none",
                )
                if not retryable:
                    raise VoiceError(
                        f"Gemini rejected the speech request (HTTP {response.status_code})."
                    )
                last_error = VoiceError(
                    f"Gemini rejected the speech request (HTTP {response.status_code})."
                )
                if response.status_code == 429 and stage == "stt":
                    # Remember the model, not just the stage: the next utterance
                    # can skip it entirely instead of failing again.
                    self._model_blocked_until[model] = time.monotonic() + 60.0
                if attempt >= attempts:
                    break
                delay = self._backoff_delay(attempt, retry_after)
                cooldown_cap = _MAX_COOLDOWN_BY_STAGE.get(
                    stage, _MAX_COOLDOWN_WAIT_SECONDS
                )
                if deadline is not None and time.monotonic() + delay >= deadline:
                    logger.warning(
                        "Gemini %s: stopping retries, the %.1fs budget is used up",
                        label,
                        self._settings.gemini_stt_total_seconds,
                    )
                    break
                if response.status_code == 429:
                    # Other callers of this stage wait too, instead of
                    # stampeding the quota; this caller sleeps only the part
                    # the cooldown does not cover (no double waiting).
                    self._blocked_until[stage] = time.monotonic() + min(
                        delay, cooldown_cap
                    )

                    remainder = delay - cooldown_cap
                    if remainder > 0:
                        await _sleep(remainder)
                else:
                    await _sleep(delay)
            raise VoiceError("Speech request failed. Please try again.") from last_error
        finally:
            if owns_client:
                await client.aclose()

    async def _wait_for_cooldown(
        self, stage: str, deadline: float | None = None
    ) -> None:
        cap = _MAX_COOLDOWN_BY_STAGE.get(stage, _MAX_COOLDOWN_WAIT_SECONDS)
        wait = self._blocked_until.get(stage, 0.0) - time.monotonic()
        if wait <= 0:
            return
        if deadline is not None:
            wait = min(wait, max(0.0, deadline - time.monotonic()))
        if wait > 0:
            await _sleep(min(wait, cap))

    @staticmethod
    def _backoff_delay(attempt: int, retry_after: float | None = None) -> float:
        base = min(
            _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS
        )
        delay = base
        if retry_after is not None:
            delay = max(delay, min(retry_after, _MAX_RETRY_AFTER_SECONDS))
        return round(delay + random.uniform(0, delay * 0.25), 3)

    @staticmethod
    def _cache_key(stage: str, model: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(f"{stage}:{model}:{body}".encode()).hexdigest()

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return data

    def _cache_put(self, key: str, data: dict[str, Any]) -> None:
        self._cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, data)
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    # -- parsing ----------------------------------------------------------- #
    @staticmethod
    def _first_candidate(data: dict[str, Any]) -> dict[str, Any]:
        candidates = data.get("candidates") or []
        if not candidates:
            raise VoiceError("Gemini returned no speech result.")
        return candidates[0]

    def _transcript_from(self, data: dict[str, Any]) -> str:
        content = self._first_candidate(data).get("content") or {}
        pieces: list[str] = []
        for part in content.get("parts") or []:
            transcription = part.get("audioTranscription") or {}
            text = transcription.get("text") or part.get("text") or ""
            if text:
                pieces.append(str(text))
        return " ".join(piece.strip() for piece in pieces if piece.strip()).strip()

    def _audio_from(self, data: dict[str, Any]) -> bytes:
        content = self._first_candidate(data).get("content") or {}
        for part in content.get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"])
                except (ValueError, TypeError) as exc:
                    raise VoiceError("Gemini returned invalid audio data.") from exc
        raise VoiceError("Gemini returned no audio.")

    # -- audio conversion (runs in a worker thread) ------------------------ #
    def _to_wav(self, audio: bytes) -> bytes:
        if audio[:4] == b"RIFF":
            return audio
        return self._ffmpeg(
            ["-vn", "-ar", str(_PCM_SAMPLE_RATE), "-ac", "1", "-f", "wav", "pipe:1"],
            audio,
        )

    def _wav_to_pcm(self, wav: bytes) -> bytes:
        try:
            with wave.open(io.BytesIO(wav), "rb") as handle:
                if (
                    handle.getframerate() == _PCM_SAMPLE_RATE
                    and handle.getnchannels() == 1
                    and handle.getsampwidth() == 2
                ):
                    return handle.readframes(handle.getnframes())
        except (wave.Error, EOFError):
            raise VoiceError("Gemini returned an unreadable audio container.")

        converted = self._ffmpeg(
            ["-vn", "-ar", str(_PCM_SAMPLE_RATE), "-ac", "1", "-f", "s16le", "pipe:1"],
            wav,
        )
        return converted

    def _pcm_to_mp3(self, pcm: bytes) -> bytes:
        return self._ffmpeg(
            [
                "-f",
                "s16le",
                "-ar",
                str(_PCM_SAMPLE_RATE),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-f",
                "mp3",
                "pipe:1",
            ],
            pcm,
        )

    @staticmethod
    def _ffmpeg(arguments: list[str], source: bytes) -> bytes:
        executable = ffmpeg_path()
        if executable is None:
            raise VoiceError("FFmpeg is required to convert this audio.")
        command = [executable, "-hide_banner", "-loglevel", "error", *arguments]
        try:
            completed = subprocess.run(
                command,
                input=source,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceError("Audio conversion failed or timed out.") from exc
        if completed.returncode != 0 or not completed.stdout:
            raise VoiceError("Audio conversion produced no output.")
        return completed.stdout
