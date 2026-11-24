"""Shared pytest fixtures.

Tests never touch the real database, never call OpenAI and never need
Telegram credentials: every credential-shaped environment variable is
removed and the database points at a per-test temporary file.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import config
import database
from config import Settings


@pytest.fixture(autouse=True)
def _clean_database_state() -> Iterator[None]:
    database.dispose_database()
    yield
    database.dispose_database()


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool calls made by tests must not appear in the owner's audit feed."""
    import safety

    monkeypatch.setattr(safety, "TRASH_DIR", tmp_path / "trash")
    monkeypatch.setattr(safety, "AUDIT_FILE", tmp_path / "audit.jsonl")


@pytest.fixture(autouse=True)
def _isolated_key_pool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never read or rotate the owner's real API keys."""
    import api_keys

    monkeypatch.setattr(api_keys, "POOL_PATH", tmp_path / "api_keys.json")


@pytest.fixture()
def settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Settings pointing at a throwaway database with no external services."""
    db_file = tmp_path / "agent-test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    # Tests must never write into the production logs/agent.log: that file is
    # how the owner diagnoses real calls, and test noise ruins it.
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CHAT_CONTEXT_MESSAGES", "20")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "100")
    monkeypatch.setenv("REMINDER_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("REMINDER_RETRY_MINUTES", "1")
    monkeypatch.setenv("OPENAI_RETRY_BASE_DELAY", "0")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "2")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("VOICE_MAX_UPLOAD_MB", "5")

    for name in (
        "OPENAI_API_KEY",
        "API_TOKEN",
        "SYSTEM_PROMPT",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_PHONE",
        "TELEGRAM_OWNER_USER_ID",
        "OPENROUTER_API_KEY",
        "EXA_API_KEY",
        "APY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    config.get_settings.cache_clear()
    database.dispose_database()
    try:
        yield Settings(_env_file=None)
    finally:
        database.dispose_database()
        config.get_settings.cache_clear()
