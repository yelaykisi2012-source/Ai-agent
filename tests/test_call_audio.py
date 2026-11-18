"""PCM helpers for Telegram call audio. No device, no network."""

from __future__ import annotations

import io
import wave
from array import array

from telegram.audio import (
    BYTES_PER_FRAME,
    BYTES_PER_FRAME as FRAME,
    CALL_SAMPLE_RATE,
    SILENCE_FRAME,
    is_speech,
    pcm16_to_wav,
    pcm_duration_ms,
    rms,
    silence,
    split_frames,
    upsample_24k_to_48k,
)


def _tone(amplitude: int = 8000, samples: int = 480) -> bytes:
    return array("h", [amplitude] * samples).tobytes()


def test_frames_are_ten_milliseconds_of_pcm16() -> None:
    assert CALL_SAMPLE_RATE == 48_000
    assert BYTES_PER_FRAME == 960
    assert len(SILENCE_FRAME) == FRAME


def test_split_pads_the_last_frame() -> None:
    frames = split_frames(b"\x01\x00" * 500)
    assert len(frames) == 2
    assert all(len(frame) == FRAME for frame in frames)
    assert frames[0] == b"\x01\x00" * 480
    assert frames[1].endswith(b"\x00" * (FRAME - 40))


def test_silence_has_no_energy() -> None:
    assert rms(SILENCE_FRAME) == 0.0
    assert is_speech(SILENCE_FRAME, 100) is False
    assert len(silence(20)) == 2 * FRAME


def test_loud_pcm_is_detected_as_speech() -> None:
    frame = _tone(8000)
    assert rms(frame) > 1000
    assert is_speech(frame, 400) is True


def test_wav_wrapper_has_the_call_format() -> None:
    pcm = _tone()
    data = pcm16_to_wav(pcm)
    with wave.open(io.BytesIO(data), "rb") as handle:
        assert handle.getframerate() == CALL_SAMPLE_RATE
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.readframes(10_000) == pcm


def test_upsample_doubles_every_sample() -> None:
    pcm = array("h", [100, -100, 200]).tobytes()
    doubled = array("h")
    doubled.frombytes(upsample_24k_to_48k(pcm))
    assert list(doubled) == [100, 100, -100, -100, 200, 200]
    assert len(upsample_24k_to_48k(pcm)) == len(pcm) * 2


def test_duration_reflects_sample_rate() -> None:
    assert pcm_duration_ms(SILENCE_FRAME) == 10
    assert pcm_duration_ms(b"") == 0


def test_silence_padding_is_trimmed_from_a_sentence() -> None:
    from array import array

    from telegram.audio import trim_silence_edges

    pad = array("h", [0] * 2400).tobytes()  # 100 ms of silence
    speech = array("h", [8000, -8000] * 2400).tobytes()  # 200 ms of "speech"
    trimmed = trim_silence_edges(pad + speech + pad)

    # Leading and trailing padding is gone, with only the 30 ms safety margin.
    assert len(trimmed) < len(pad + speech + pad)
    assert len(trimmed) >= len(speech)
    assert trim_silence_edges(pad) == b""
