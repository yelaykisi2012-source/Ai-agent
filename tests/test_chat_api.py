"""Chat, confirmation and dashboard HTTP tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from brain import AgentBrain
from config import Settings
from fakes import (
    FakeOpenAI,
    FakeSpeech,
    FakeTelegram,
    auth_error,
    text_completion,
    timeout_error,
    tool_completion,
)
from main import create_app
from runtime import AgentRuntime


def _client(
    settings: Settings,
    responses: list[object],
    *,
    speech: FakeSpeech | None = None,
    telegram: FakeTelegram | None = None,
) -> TestClient:
    brain = AgentBrain(settings, client=FakeOpenAI(responses))
    runtime = AgentRuntime(
        settings,
        brain=brain,
        telegram=telegram or FakeTelegram(),
        speech=speech or FakeSpeech(),
    )
    return TestClient(create_app(settings, runtime=runtime))


def test_chat_creates_conversation_and_persists_turn(settings: Settings) -> None:
    with _client(settings, [text_completion("Assalomu alaykum!")]) as client:
        response = client.post("/chat", json={"message": "Salom"})
        assert response.status_code == 200
        body = response.json()
        assert body["reply"] == "Assalomu alaykum!"
        assert body["conversation_id"] >= 1
        assert body["usage"]["total_tokens"] == 20

        detail = client.get(f"/conversations/{body['conversation_id']}").json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert detail["messages"][0]["content"] == "Salom"

        listed = client.get("/conversations").json()
        assert listed["total"] == 1
        assert listed["conversations"][0]["message_count"] == 2


def test_chat_sends_previous_turns_as_context(settings: Settings) -> None:
    fake = FakeOpenAI([text_completion("Birinchi javob"), text_completion("Ikkinchi javob")])
    brain = AgentBrain(settings, client=fake)
    app = create_app(
        settings,
        runtime=AgentRuntime(
            settings, brain=brain, telegram=FakeTelegram(), speech=FakeSpeech()
        ),
    )
    with TestClient(app) as client:
        first = client.post("/chat", json={"message": "Birinchi savol"}).json()
        second = client.post(
            "/chat",
            json={"message": "Ikkinchi savol", "conversation_id": first["conversation_id"]},
        )
        assert second.status_code == 200

        messages = fake.requests[1]["messages"]
        contents = [m.get("content") for m in messages]
        assert "Birinchi savol" in contents
        assert "Birinchi javob" in contents
        assert messages[-1]["content"] == "Ikkinchi savol"


def test_chat_rejects_blank_and_unknown(settings: Settings) -> None:
    with _client(settings, []) as client:
        assert client.post("/chat", json={"message": "   "}).status_code == 422
        assert client.post("/chat", json={"message": "hi", "conversation_id": 9999}).status_code == 404
        assert client.post("/chat", json={"message": "hi", "conversation_id": "abc"}).status_code == 422


def test_chat_maps_provider_timeout_to_504(settings: Settings) -> None:
    attempts = settings.openai_max_retries + 1
    with _client(settings, [timeout_error() for _ in range(attempts)]) as client:
        response = client.post("/chat", json={"message": "Salom"})
    assert response.status_code == 504
    assert "too long" in response.json()["detail"].lower()


def test_chat_maps_auth_error_to_503(settings: Settings) -> None:
    with _client(settings, [auth_error()]) as client:
        response = client.post("/chat", json={"message": "Salom"})
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_chat_returns_confirmation_for_sensitive_tool(settings: Settings) -> None:
    responses = [
        tool_completion("initiate_telegram_call", '{"reason": "wake me up"}'),
        text_completion("I need your approval before calling you."),
    ]
    with _client(settings, responses) as client:
        response = client.post("/chat", json={"message": "Call me now"})
        assert response.status_code == 200
        body = response.json()
        confirmation = body["confirmation"]
        assert confirmation is not None
        assert confirmation["tool"] == "initiate_telegram_call"
        assert body["tools"][0]["status"] == "confirmation_required"

        confirmed = client.post(f"/confirmations/{confirmation['id']}")
        assert confirmed.status_code == 200
        result = confirmed.json()
        assert result["status"] == "ok"
        assert "cannot" in result["detail"].lower()

        # Single use: the same approval cannot fire again.
        assert client.post(f"/confirmations/{confirmation['id']}").status_code == 404


def test_chat_rate_limit(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")
    limited = Settings(_env_file=None)
    with _client(limited, [text_completion("a"), text_completion("b")]) as client:
        assert client.post("/chat", json={"message": "one"}).status_code == 200
        assert client.post("/chat", json={"message": "two"}).status_code == 200
        assert client.post("/chat", json={"message": "three"}).status_code == 429


def test_dashboard_and_summary(settings: Settings) -> None:
    with _client(settings, []) as client:
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "KSENUS" in page.text

        summary = client.get("/dashboard/summary")
        assert summary.status_code == 200
        body = summary.json()
        assert body["agent"] == settings.app_name
        assert "telegram_calls_audio" in body["services"]
        assert body["services"]["telegram_calls_audio"] is False
        serialized = summary.text.lower()
        assert "session" not in serialized
        assert "api_token" not in serialized
