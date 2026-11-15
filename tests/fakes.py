"""Test doubles for external services.

No test in this project contacts OpenAI, Telegram or a speech API.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx2
from openai import (
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)


# --------------------------------------------------------------------------- #
# OpenAI chat completions
# --------------------------------------------------------------------------- #
@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = None


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class FakeUsage:
    prompt_tokens: int = 12
    completion_tokens: int = 8
    total_tokens: int = 20


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    model: str = "fake-model"
    usage: FakeUsage | None = None


def text_completion(text: str, *, model: str = "fake-model") -> FakeCompletion:
    return FakeCompletion(
        choices=[FakeChoice(message=FakeMessage(content=text))],
        model=model,
        usage=FakeUsage(),
    )


def tool_completion(
    name: str,
    arguments: str,
    *,
    call_id: str = "call_1",
    text: str | None = None,
) -> FakeCompletion:
    return FakeCompletion(
        choices=[
            FakeChoice(
                message=FakeMessage(
                    content=text,
                    tool_calls=[
                        FakeToolCall(
                            id=call_id, function=FakeFunction(name=name, arguments=arguments)
                        )
                    ],
                )
            )
        ],
        usage=FakeUsage(),
    )


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self.queue:
            raise AssertionError("FakeOpenAI received an unexpected request")
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeOpenAI:
    """Minimal stand-in exposing ``chat.completions.create``."""

    def __init__(self, responses: list[Any]) -> None:
        self.chat = FakeChat(FakeCompletions(responses))

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.chat.completions.requests


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _response(status_code: int) -> httpx2.Response:
    return httpx2.Response(
        status_code, request=_request(), json={"error": {"message": "stub"}}
    )


def timeout_error() -> APITimeoutError:
    return APITimeoutError(request=_request())


def rate_limit_error() -> RateLimitError:
    return RateLimitError("rate limited", response=_response(429), body=None)


def auth_error() -> AuthenticationError:
    return AuthenticationError("invalid key", response=_response(401), body=None)


def bad_request_error() -> BadRequestError:
    return BadRequestError("bad model", response=_response(400), body=None)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
class FakeTelegram:
    """Stands in for the Telethon-backed account."""

    def __init__(self, *, ready: bool = False, send_ok: bool = True) -> None:
        self.ready = ready
        self.send_ok = send_ok
        self.sent: list[str] = []
        self.started = False
        self.client = None

    async def start(self) -> str:
        self.started = True
        return "connected" if self.ready else "disabled"

    async def stop(self) -> None:
        self.started = False

    async def send_message(self, text: str, user_id: int | None = None) -> bool:
        self.sent.append(text)
        return self.send_ok and self.ready

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "configured": True,
            "connected": self.ready,
            "authorized": self.ready,
            "owner_configured": True,
            "live_calls_enabled": False,
            "detail": "fake",
        }


def fake_call_update(
    constructor: str = "PhoneCallRequested",
    *,
    call_id: int = 777,
    admin_id: int = 111,
    participant_id: int = 222,
    access_hash: int = 333,
) -> SimpleNamespace:
    """Build an object whose constructor name matches a Telegram TL type."""

    call_type = type(constructor, (), {})
    call = call_type()
    call.id = call_id
    call.admin_id = admin_id
    call.participant_id = participant_id
    call.access_hash = access_hash

    update_type = type("UpdatePhoneCall", (), {})
    update = update_type()
    update.phone_call = call
    return update


# --------------------------------------------------------------------------- #
# Speech
# --------------------------------------------------------------------------- #
class FakeSpeech:
    """Stands in for the OpenAI speech engine."""

    def __init__(self, transcript: str = "Salom", audio: bytes = b"ID3fake-mp3") -> None:
        self.transcript = transcript
        self.audio = audio
        self.transcribed: list[bytes] = []
        self.synthesized: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    async def transcribe(
        self, audio: bytes, filename: str = "utterance.webm", language: str | None = None
    ) -> str:
        self.transcribed.append(audio)
        return self.transcript

    async def synthesize(self, text: str) -> bytes:
        self.synthesized.append(text)
        return self.audio
