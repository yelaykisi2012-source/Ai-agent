"""Raw PCM helpers for Telegram call audio.

Telegram 1:1 calls carry 48 kHz mono signed-16-bit little-endian PCM in 10 ms
frames (480 samples, 960 bytes). These helpers build and inspect those frames
without any extra dependency: no FFmpeg and no native audio library is needed
for the call path itself.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from collections.abc import Iterable

CALL_SAMPLE_RATE = 48_000
CALL_CHANNELS = 1
CALL_SAMPLE_WIDTH = 2  # int16
FRAME_DURATION_MS = 10
SAMPLES_PER_FRAME = CALL_SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 480
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CALL_SAMPLE_WIDTH  # 960

SILENCE_FRAME: bytes = b"\x00" * BYTES_PER_FRAME

#: OpenAI speech synthesis returns 24 kHz PCM; doubling every sample is an
#: exact 2x upsample to Telegram's 48 kHz and needs no resampling library.
TTS_SAMPLE_RATE = 24_000


def split_frames(pcm: bytes) -> list[bytes]:
    """Split PCM into 10 ms frames, padding the last one with silence."""
    frames: list[bytes] = []
    for start in range(0, len(pcm), BYTES_PER_FRAME):
        chunk = pcm[start : start + BYTES_PER_FRAME]
        if len(chunk) < BYTES_PER_FRAME:
            chunk += b"\x00" * (BYTES_PER_FRAME - len(chunk))
        frames.append(chunk)
    return frames


def pcm_duration_ms(pcm: bytes) -> float:
    """Duration of a PCM blob in milliseconds."""
    samples = len(pcm) // CALL_SAMPLE_WIDTH
    return samples * 1000 / CALL_SAMPLE_RATE


def rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of int16 PCM (0..32767)."""
    usable = len(pcm) - (len(pcm) % CALL_SAMPLE_WIDTH)
    if usable <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if not samples:
        return 0.0
    total = sum(sample * sample for sample in samples)
    return math.sqrt(total / len(samples))


def is_speech(pcm: bytes, threshold: float) -> bool:
    """True when the frame carries enough energy to count as speech."""
    return rms(pcm) >= threshold


def silence(duration_ms: int) -> bytes:
    """Silent PCM of the requested duration (rounded to whole frames)."""
    frames = max(1, round(duration_ms / FRAME_DURATION_MS))
    return b"\x00" * (frames * BYTES_PER_FRAME)


def pcm16_to_wav(pcm: bytes, sample_rate: int = CALL_SAMPLE_RATE) -> bytes:
    """Wrap PCM in a RIFF/WAVE container so speech APIs can read it."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(CALL_CHANNELS)
        handle.setsampwidth(CALL_SAMPLE_WIDTH)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def upsample_24k_to_48k(pcm: bytes) -> bytes:
    """Exact 2x upsample of int16 mono PCM (24 kHz -> 48 kHz)."""
    usable = len(pcm) - (len(pcm) % CALL_SAMPLE_WIDTH)
    if usable <= 0:
        return b""
    samples = array("h")
    samples.frombytes(pcm[:usable])
    doubled = array("h", bytes(len(samples) * 4))
    index = 0
    for sample in samples:
        doubled[index] = sample
        doubled[index + 1] = sample
        index += 2
    return doubled.tobytes()


def trim_silence_edges(
    pcm: bytes,
    *,
    sample_rate: int = TTS_SAMPLE_RATE,
    threshold: int = 300,
    keep_ms: int = 30,
) -> bytes:
    """Trim near-silent padding from both ends of one synthesized sentence.

    The TTS provider pads every request with a few hundred milliseconds of
    silence. Synthesizing a reply sentence by sentence would add that padding
    between sentences, which sounds choppy and makes Ksenus speak much slower
    than the voice actually is. A short margin is kept so the first consonant
    is not clipped.
    """
    usable = len(pcm) - (len(pcm) % CALL_SAMPLE_WIDTH)
    if usable <= 0:
        return b""
    samples = array("h")
    samples.frombytes(pcm[:usable])

    start = 0
    while start < len(samples) and abs(samples[start]) < threshold:
        start += 1
    end = len(samples)
    while end > start and abs(samples[end - 1]) < threshold:
        end -= 1
    if start >= end:
        return b""

    margin = sample_rate * keep_ms // 1000
    start = max(0, start - margin)
    end = min(len(samples), end + margin)
    return pcm[start * CALL_SAMPLE_WIDTH : end * CALL_SAMPLE_WIDTH]


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """Scale PCM amplitude, clipping to the int16 range."""
    usable = len(pcm) - (len(pcm) % CALL_SAMPLE_WIDTH)
    if usable <= 0 or gain == 1.0:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[:usable])
    for index, sample in enumerate(samples):
        scaled = int(sample * gain)
        samples[index] = max(-32768, min(32767, scaled))
    return samples.tobytes()


def concat(chunks: Iterable[bytes]) -> bytes:
    """Join PCM chunks into one blob."""
    return b"".join(chunks)


def frame_iter(pcm: bytes) -> Iterator[bytes]:
    """Yield padded 10 ms frames."""
    yield from split_frames(pcm)
