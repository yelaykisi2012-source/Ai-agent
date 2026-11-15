"""Configuration for MyAIAgent.

Every runtime value is read from an environment variable, optionally supplied
through a local ``.env`` file. Settings are validated once at startup so a
typo (bad timezone, impossible port, unknown log level) fails immediately
instead of at 3 a.m. while a task reminder is firing.

Environment variables take precedence over ``.env`` values, which take
precedence over the defaults below.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Directory that contains the Python modules (the project root).
PROJECT_ROOT: Path = Path(__file__).resolve().parent

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_ENVIRONMENTS = {"development", "production"}


class Settings(BaseSettings):
    """Validated application settings.

    Environment variable names are the upper-case field names, for example
    ``TIMEZONE=Asia/Tashkent`` or ``API_PORT=9000``.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "MyAIAgent"
    environment: str = "development"
    log_level: str = "INFO"
    log_dir: str = "logs"

    #: IANA timezone used for user-facing reminders and schedule display.
    timezone: str = "Asia/Tashkent"

    #: SQLAlchemy URL. Relative SQLite paths resolve against PROJECT_ROOT.
    database_url: str = "sqlite:///./data/agent.db"

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # --- OpenAI ------------------------------------------------------------
    #: Read from OPENAI_API_KEY. Empty values are normalised to None so an
    #: unset key is detected reliably. Never logged or serialised.
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: Extra attempts after the first failed call (0 disables retries).
    openai_max_retries: int = Field(default=3, ge=0, le=6)
    openai_retry_base_delay: float = Field(default=0.5, ge=0, le=30)
    openai_retry_max_delay: float = Field(default=8.0, ge=0, le=60)
    #: Maximum model<->tool round trips for a single reply.
    openai_max_tool_rounds: int = Field(default=3, ge=1, le=5)

    # --- Conversation ------------------------------------------------------
    #: How many previous messages are sent as context on each turn.
    chat_context_messages: int = Field(default=20, ge=1, le=100)
    #: Optional replacement persona. Safety rules are always appended.
    system_prompt: str | None = None

    # --- Voice -------------------------------------------------------------
    openai_stt_model: str = "whisper-1"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    voice_max_upload_mb: int = Field(default=15, ge=1, le=50)

    # --- Telegram user account --------------------------------------------
    #: Ksenus is a user account, never a bot. All values are optional so the
    #: rest of the agent runs before Telegram is configured.
    telegram_enabled: bool = False
    telegram_api_id: int | None = Field(default=None, ge=1)
    telegram_api_hash: SecretStr | None = None
    telegram_phone: str | None = None
    #: Session file (without extension); created by the login CLI.
    telegram_session_path: str = "data/telegram/ksenus"
    #: Only this Telegram user id may talk to Ksenus. Set by the login CLI.
    telegram_owner_user_id: int | None = None
    #: Accept incoming calls without asking. Requires a working media stack.
    telegram_allow_incoming_calls: bool = False
    #: Enables the native call transport. Off until verified on this machine.
    telegram_calls_enabled: bool = False

    # --- Autonomy and security --------------------------------------------
    #: When set, mutating endpoints require this token in the X-API-Token header.
    api_token: SecretStr | None = None
    chat_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    reminder_max_attempts: int = Field(default=3, ge=1, le=10)
    reminder_retry_minutes: int = Field(default=5, ge=1, le=120)
    dashboard_enabled: bool = True

    @field_validator("environment")
    @classmethod
    def _check_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {sorted(_VALID_ENVIRONMENTS)}, got {value!r}"
            )
        return normalized

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
            )
        return normalized

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("database_url must not be empty")
        return value.strip()

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("openai_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("openai_model must not be empty")
        return normalized

    @field_validator("system_prompt")
    @classmethod
    def _normalise_system_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("telegram_api_hash", "api_token", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("telegram_phone")
    @classmethod
    def _normalise_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip().replace(" ", "")
        return stripped or None

    @field_validator("openai_stt_model", "openai_tts_model", "openai_tts_voice")
    @classmethod
    def _check_voice_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def tzinfo(self) -> ZoneInfo:
        """Resolved timezone object for date arithmetic."""
        return ZoneInfo(self.timezone)

    @property
    def log_path(self) -> Path:
        """Absolute directory for log files."""
        directory = Path(self.log_dir)
        return directory if directory.is_absolute() else PROJECT_ROOT / directory

    @property
    def openai_configured(self) -> bool:
        """True when an API key is present. Safe to log."""
        return self.openai_api_key is not None

    @property
    def openai_key(self) -> str | None:
        """The raw key for the SDK client. Never log or serialise this."""
        return self.openai_api_key.get_secret_value() if self.openai_api_key else None

    @property
    def api_token_value(self) -> str | None:
        """Raw API token. Never log or serialise this."""
        return self.api_token.get_secret_value() if self.api_token else None

    @property
    def telegram_configured(self) -> bool:
        """True when api_id and api_hash are present. Safe to log."""
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def telegram_session_file(self) -> Path:
        """Absolute path (no extension) for the Telethon session file."""
        path = Path(self.telegram_session_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that repeated FastAPI dependency calls do not re-parse ``.env``.
    Tests call ``get_settings.cache_clear()`` after changing the environment.
    """
    return Settings()
