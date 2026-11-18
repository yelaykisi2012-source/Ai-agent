"""Settings validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import Settings


def test_defaults_are_sane() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_name == "MyAIAgent"
    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.database_url.startswith("sqlite:///")
    assert settings.api_port == 8000
    assert settings.tzinfo.key == settings.timezone
    assert settings.is_production is False


def test_log_level_is_normalised() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="LOUD")


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, timezone="Mars/Olympus_Mons")


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="staging")


def test_invalid_port_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=70000)


def test_get_settings_is_cached() -> None:
    import config

    config.get_settings.cache_clear()
    try:
        assert config.get_settings() is config.get_settings()
    finally:
        config.get_settings.cache_clear()


def test_llm_provider_defaults_to_openai_and_switches_the_model() -> None:
    default = Settings(_env_file=None)
    assert default.llm_provider == "openai"
    assert default.chat_model == default.openai_model

    openrouter = Settings(
        _env_file=None,
        llm_provider="openrouter",
        openrouter_model="google/gemma-4-31b-it:free",
    )
    assert openrouter.chat_model == "google/gemma-4-31b-it:free"
    assert openrouter.llm_configured is False


def test_unknown_llm_provider_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="anthropic")


def test_openrouter_defaults_are_free_and_openai_compatible() -> None:
    settings = Settings(_env_file=None)
    assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert settings.openrouter_model == "openrouter/free"
    assert settings.openrouter_configured is False


def test_openrouter_key_is_secret_and_blank_is_none() -> None:
    blank = Settings(_env_file=None, openrouter_api_key="   ")
    assert blank.openrouter_configured is False
    assert blank.openrouter_key is None

    key = "sk-or-v1-unit-test"
    configured = Settings(
        _env_file=None, openrouter_api_key=key, openai_api_key=None
    )
    assert configured.openrouter_configured is True
    assert configured.llm_configured is False  # provider is still "openai"
    assert configured.openrouter_key == key
    assert key not in repr(configured)


def test_openrouter_base_url_must_be_http() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openrouter_base_url="openrouter.ai/api/v1")
