"""Brain tests with a fake OpenAI client. No network, no API key."""

from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import SecretStr

from brain import (
    AgentBrain,
    BrainNotConfigured,
    BrainTimeout,
    BrainUnavailable,
    STATUS_CONFIRMATION_REQUIRED,
    STATUS_ERROR,
    STATUS_UNAVAILABLE,
    ToolRegistry,
    ToolSpec,
)
from config import Settings
from fakes import (
    FakeOpenAI,
    auth_error,
    bad_request_error,
    rate_limit_error,
    text_completion,
    timeout_error,
    tool_completion,
)


def _brain(settings: Settings, responses: list[object]) -> tuple[AgentBrain, FakeOpenAI]:
    fake = FakeOpenAI(responses)
    return AgentBrain(settings, client=fake), fake


def test_reply_uses_context_and_system_prompt(settings: Settings) -> None:
    brain, fake = _brain(settings, [text_completion("Assalomu alaykum!")])
    reply = asyncio.run(
        brain.reply(
            history=[
                {"role": "user", "content": "Salom"},
                {"role": "assistant", "content": "Salom! Qanday yordam bera olaman?"},
            ],
            user_message="Ertaga soat 9 da meni uyg'ot.",
        )
    )
    assert reply.text == "Assalomu alaykum!"
    sent = fake.requests[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "Uzbek" in sent[0]["content"]
    assert settings.timezone in sent[0]["content"]
    assert sent[-1] == {"role": "user", "content": "Ertaga soat 9 da meni uyg'ot."}
    assert any(turn.get("content") == "Salom" for turn in sent[1:])


def test_context_limit_trims_history(settings: Settings) -> None:
    trimmed = settings.model_copy(update={"chat_context_messages": 4})
    brain, fake = _brain(trimmed, [text_completion("ok")])
    history = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    asyncio.run(brain.reply(history=history, user_message="latest"))

    sent = fake.requests[0]["messages"]
    # system + last 4 history turns + current message
    assert len(sent) == 6
    assert sent[1]["content"] == "m16"
    assert sent[-1]["content"] == "latest"


def test_retryable_failure_is_retried_then_succeeds(settings: Settings) -> None:
    brain, fake = _brain(settings, [rate_limit_error(), text_completion("ok")])
    reply = asyncio.run(brain.reply(history=[], user_message="hi"))
    assert reply.text == "ok"
    assert len(fake.requests) == 2


def test_timeout_is_mapped_after_exhausting_retries(settings: Settings) -> None:
    attempts = settings.openai_max_retries + 1
    brain, fake = _brain(settings, [timeout_error() for _ in range(attempts)])
    with pytest.raises(BrainTimeout):
        asyncio.run(brain.reply(history=[], user_message="hi"))
    assert len(fake.requests) == attempts


def test_authentication_error_is_not_retried(settings: Settings) -> None:
    brain, fake = _brain(settings, [auth_error(), text_completion("unused")])
    with pytest.raises(BrainNotConfigured):
        asyncio.run(brain.reply(history=[], user_message="hi"))
    assert len(fake.requests) == 1


def test_bad_request_is_reported_safely(settings: Settings) -> None:
    brain, _ = _brain(settings, [bad_request_error()])
    with pytest.raises(BrainUnavailable) as excinfo:
        asyncio.run(brain.reply(history=[], user_message="hi"))
    assert "OPENAI_MODEL" in str(excinfo.value)


def test_empty_reply_is_rejected(settings: Settings) -> None:
    brain, _ = _brain(settings, [text_completion("   ")])
    with pytest.raises(BrainUnavailable):
        asyncio.run(brain.reply(history=[], user_message="hi"))


def test_missing_key_reports_not_configured(settings: Settings) -> None:
    brain = AgentBrain(settings)  # no injected client, no key in test env
    with pytest.raises(BrainNotConfigured):
        asyncio.run(brain.reply(history=[], user_message="hi"))


def test_unimplemented_tools_are_not_advertised(settings: Settings) -> None:
    brain, fake = _brain(
        settings,
        [
            tool_completion("create_task", '{"title": "Study"}'),
            text_completion("The task manager is not available."),
        ],
    )
    reply = asyncio.run(brain.reply(history=[], user_message="create a task"))

    assert "tools" not in fake.requests[0]
    assert reply.tool_results[0].status == STATUS_UNAVAILABLE
    assert "not implemented" in reply.tool_results[0].detail.lower()
    tool_messages = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"]
    assert tool_messages and "unavailable" in tool_messages[0]["content"]


def test_invalid_tool_arguments_are_not_executed(settings: Settings) -> None:
    registry = ToolRegistry()
    executed: list[dict[str, object]] = []

    async def handler(arguments: dict[str, object]) -> str:
        executed.append(arguments)
        return "ran"

    registry.register(
        ToolSpec(
            name="demo",
            description="demo",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
    )
    brain = AgentBrain(
        settings,
        client=FakeOpenAI(
            [
                tool_completion("demo", "{not json"),
                text_completion("I could not do that."),
            ]
        ),
        registry=registry,
    )
    reply = asyncio.run(brain.reply(history=[], user_message="run demo"))
    assert executed == []
    assert reply.tool_results[0].status == STATUS_ERROR


def test_confirmation_gating_never_executes_early(settings: Settings) -> None:
    registry = ToolRegistry()
    executed: list[str] = []

    async def handler(arguments: dict[str, object]) -> str:
        executed.append("called")
        return "called the phone"

    registry.register(
        ToolSpec(
            name="make_call",
            description="call",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            requires_confirmation=True,
        )
    )

    first = asyncio.run(registry.execute("make_call", {}))
    assert first.status == STATUS_CONFIRMATION_REQUIRED
    assert executed == []

    second = asyncio.run(registry.execute("make_call", {}, confirmed=True))
    assert second.status == "ok"
    assert executed == ["called"]


def test_unknown_tool_is_refused(settings: Settings) -> None:
    registry = ToolRegistry()
    result = asyncio.run(registry.execute("delete_everything", {}))
    assert result.status == STATUS_ERROR
    assert "Unknown tool" in result.detail


def test_tool_round_limit_stops_runaway_model(settings: Settings) -> None:
    rounds = settings.openai_max_tool_rounds
    brain, _ = _brain(
        settings,
        [tool_completion("create_task", '{"title": "x"}') for _ in range(rounds)],
    )
    with pytest.raises(BrainUnavailable):
        asyncio.run(brain.reply(history=[], user_message="loop"))


# --------------------------------------------------------------------------- #
# OpenRouter provider
# --------------------------------------------------------------------------- #
def _openrouter_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "llm_provider": "openrouter",
        "openrouter_api_key": "sk-or-v1-unit-test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_openrouter_client_uses_its_own_base_url_key_and_model() -> None:
    settings = _openrouter_settings(openrouter_model="google/gemma-4-31b-it:free")
    brain = AgentBrain(settings)
    client = brain._ensure_client()

    assert str(client.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
    assert client.api_key == "sk-or-v1-unit-test"
    assert brain.model == "google/gemma-4-31b-it:free"


def test_openrouter_falls_back_to_the_openai_provider() -> None:
    settings = _openrouter_settings(
        openrouter_model="openrouter/free",
        openai_api_key="unit-openai-key",
        openai_model="gpt-fallback",
    )
    primary = FakeOpenAI([auth_error()])
    fallback = FakeOpenAI([text_completion("fallback answer")])
    brain = AgentBrain(settings, client=primary, fallback_client=fallback)

    reply = asyncio.run(brain.reply(history=[], user_message="hi"))

    assert reply.text == "fallback answer"
    assert primary.requests[0]["model"] == "openrouter/free"
    assert fallback.requests[0]["model"] == "gpt-fallback"
    assert len(primary.requests) == 1
    assert len(fallback.requests) == 1


def test_openrouter_failure_without_a_fallback_key_is_reported() -> None:
    brain = AgentBrain(
        _openrouter_settings(openai_api_key=None),
        client=FakeOpenAI([auth_error()]),
    )
    with pytest.raises(BrainNotConfigured):
        asyncio.run(brain.reply(history=[], user_message="hi"))


def test_openai_provider_has_no_fallback_client(settings: Settings) -> None:
    brain = AgentBrain(settings.model_copy(update={"openai_api_key": SecretStr("k")}))
    assert brain._ensure_fallback_client() is None


def test_missing_openrouter_key_names_the_variable(settings: Settings) -> None:
    settings = settings.model_copy(
        update={"llm_provider": "openrouter", "openrouter_api_key": None}
    )
    brain = AgentBrain(settings)
    with pytest.raises(BrainNotConfigured) as excinfo:
        asyncio.run(brain.reply(history=[], user_message="hi"))
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_openrouter_key_is_never_logged(caplog) -> None:
    key = "sk-or-v1-" + "a" * 32
    settings = _openrouter_settings(openrouter_api_key=key, openai_api_key=None)
    brain = AgentBrain(settings, client=FakeOpenAI([auth_error()]))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BrainNotConfigured):
            asyncio.run(brain.reply(history=[], user_message="hi"))

    assert key not in caplog.text
