"""Voice tests, including a real FFmpeg conversion."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from brain import AgentBrain
from config import Settings
from fakes import FakeOpenAI, FakeSpeech, FakeTelegram, text_completion
from main import create_app
from runtime import AgentRuntime
from voice.audio import AudioError, ensure_upload, ffmpeg_path, to_ogg_opus
from voice.speech_engine import SpeechEngine, VoiceError


def _sine_wav() -> bytes:
    executable = ffmpeg_path()
    assert executable is not None
    completed = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.4",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "pipe:1",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_upload_validation() -> None:
    with pytest.raises(AudioError):
        ensure_upload("empty.webm", b"", 5)
    with pytest.raises(AudioError):
        ensure_upload("huge.webm", b"x" * (6 * 1024 * 1024), 5)
    with pytest.raises(AudioError):
        ensure_upload("notes.txt", b"hello", 5)
    assert ensure_upload("C:\\temp\\voice.ogg", b"abc", 5) == "voice.ogg"


def test_real_ffmpeg_conversion_to_ogg_opus() -> None:
    wav = _sine_wav()
    ogg = to_ogg_opus(wav)
    assert ogg.startswith(b"OggS")
    assert len(ogg) > 100


def test_speech_engine_transcribes_and_synthesises(settings: Settings) -> None:
    class FakeTranscriptions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def create(self, **kwargs: object) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(text="  Salom, qandaysan?  ")

    class FakeSpeechApi:
        def __init__(self) -> None:
            self.transcriptions = FakeTranscriptions()

        async def create(self, **kwargs: object) -> SimpleNamespace:
            assert kwargs["response_format"] == "mp3"
            return SimpleNamespace(content=b"ID3audio")

    class FakeAudioClient:
        def __init__(self) -> None:
            self.audio = SimpleNamespace(
                transcriptions=FakeSpeechApi().transcriptions, speech=FakeSpeechApi()
            )

    engine = SpeechEngine(settings, client=FakeAudioClient())  # type: ignore[arg-type]
    assert engine.configured is True
    assert asyncio_run(engine.transcribe(b"audio", "x.webm")) == "Salom, qandaysan?"
    assert asyncio_run(engine.synthesize("Salom")) == b"ID3audio"


def asyncio_run(coroutine):  # small helper to keep imports tidy
    import asyncio

    return asyncio.run(coroutine)


def test_speech_engine_without_key_reports_unavailable(settings: Settings) -> None:
    engine = SpeechEngine(settings)
    with pytest.raises(VoiceError):
        asyncio_run(engine.transcribe(b"audio"))


def test_voice_websocket_turn(settings: Settings) -> None:
    speech = FakeSpeech(transcript="Ertaga meni uyg'ot", audio=b"ID3reply")
    brain = AgentBrain(settings, client=FakeOpenAI([text_completion("Xo'p, yozib qo'ydim.")]))
    runtime = AgentRuntime(settings, brain=brain, telegram=FakeTelegram(), speech=speech)
    app = create_app(settings, runtime=runtime)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice") as socket:
            socket.send_bytes(b"fake-webm-audio")
            socket.send_json({"type": "end"})

            transcript = socket.receive_json()
            assert transcript["type"] == "transcript"
            assert transcript["text"] == "Ertaga meni uyg'ot"

            reply = socket.receive_json()
            assert reply["type"] == "reply"
            assert reply["text"] == "Xo'p, yozib qo'ydim."

            audio = socket.receive_bytes()
            assert audio == b"ID3reply"
            assert speech.synthesized == ["Xo'p, yozib qo'ydim."]


def test_voice_websocket_rejects_empty_turn(settings: Settings) -> None:
    runtime = AgentRuntime(
        settings, telegram=FakeTelegram(), speech=FakeSpeech(transcript="")
    )
    app = create_app(settings, runtime=runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice") as socket:
            socket.send_json({"type": "end"})
            message = socket.receive_json()
            assert message["type"] == "error"
