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
