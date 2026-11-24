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

    # --- Local computer channel ------------------------------------------- #
    #: Let the owner ask the agent to read, write and list files on this
    #: computer. Everything is confined to the project folder and credential
    #: files are refused; other people (guests) never get tools at all.
    local_files_enabled: bool = True
    #: Root the file tools may touch. Empty means the project folder.
    local_files_root: str = ""
    local_files_max_kb: int = Field(default=200, ge=1, le=4096)

    # --- Computer agent ----------------------------------------------------
    #: Let the agent run shell commands and change files on this computer.
    #: Destructive commands (format, shutdown, git push, reading .env) are
    #: refused regardless; everything that is not read-only asks first.
    computer_enabled: bool = True
    #: Ask before any command that is not strictly read-only. Turn it off only
    #: if you accept that the agent may install, move and delete on its own.
    computer_confirm_commands: bool = True
    #: One permission at the start is enough. The owner's request is the
    #: approval, so a screen plan, a shell command or a file change runs at once
    #: instead of stopping for a second "ha" on every step. Destructive
    #: commands (format, shutdown, git push, reading .env) stay refused either
    #: way, and nothing runs on its own without an owner request.
    owner_auto_approve: bool = True
    computer_command_timeout: int = Field(default=60, ge=5, le=600)
    computer_output_max_kb: int = Field(default=16, ge=1, le=256)

    # --- On-screen control ------------------------------------------------- #
    #: Let the agent move the real mouse, click, type and scroll on this
    #: computer's screen, with an animated cursor on top so the owner sees it.
    #: Windows only, and every plan needs the owner's confirmation first.
    gui_control_enabled: bool = True
    #: How long the animated cursor stays on screen after the last action.
    gui_cursor_idle_seconds: int = Field(default=6, ge=2, le=60)

    # --- Web --------------------------------------------------------------- #
    #: Let the agent search the public web and read pages. Only public http(s)
    #: hosts are reachable: localhost, private ranges, metadata addresses and
    #: file:// are refused.
    web_enabled: bool = True
    web_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    web_max_results: int = Field(default=5, ge=1, le=10)
    #: Size limit for one downloaded page (also the readable text limit).
    web_fetch_max_kb: int = Field(default=64, ge=1, le=512)
    #: Optional Exa API key for real web search (https://exa.ai). Without it,
    #: search falls back to Wikipedia and DuckDuckGo's instant answer and says
    #: so, instead of pretending to cover the whole web.
    exa_api_key: SecretStr | None = None

    # --- Images (ApyHub) --------------------------------------------------- #
    #: ApyHub API token for background removal (https://apyhub.com). Empty
    #: disables the image tool honestly: no request is sent and the reply says
    #: the token is missing. Server-side only; never logged or serialised.
    apy_token: SecretStr | None = None

    #: Tool calls one reply may make before the agent must answer with what it
    #: already has. This is the run budget: it stops a loop of failing calls.
    agent_max_tool_calls: int = Field(default=8, ge=1, le=40)
    #: Tool calls allowed in one *spoken* turn. A person on the phone is waiting,
    #: so one lookup is enough; 0 answers from the model alone.
    voice_max_tool_calls: int = Field(default=1, ge=0, le=8)

    # --- OpenAI ------------------------------------------------------------
    #: Read from OPENAI_API_KEY. Empty values are normalised to None so an
    #: unset key is detected reliably. Never logged or serialised.
    openai_api_key: SecretStr | None = None
    #: Optional OpenAI-compatible base URL (for example Gemini's
    #: ``.../v1beta/openai/`` endpoint). None means api.openai.com.
    openai_base_url: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: Chat request timeout. 0 keeps OPENAI_TIMEOUT_SECONDS.
    llm_timeout_seconds: float = Field(default=0.0, ge=0, le=300)
    #: Maximum generated tokens. 0 leaves the provider default; a small value
    #: keeps replies short and makes slow free models finish sooner.
    llm_max_tokens: int = Field(default=0, ge=0, le=8192)
    #: Extra attempts after the first failed call (0 disables retries).
    openai_max_retries: int = Field(default=3, ge=0, le=6)
    openai_retry_base_delay: float = Field(default=0.5, ge=0, le=30)
    openai_retry_max_delay: float = Field(default=8.0, ge=0, le=60)
    #: How long the primary chat provider is skipped after a hard failure
    #: (daily free quota gone, rejected key). Without a fallback this is
    #: ignored. 0 disables the cooldown.
    llm_fallback_cooldown_seconds: int = Field(default=900, ge=0, le=86400)
    #: Maximum model<->tool round trips for a single reply.
    #: Model<->tool round trips per reply. Each round can request several calls,
    #: bounded separately by AGENT_MAX_TOOL_CALLS.
    openai_max_tool_rounds: int = Field(default=4, ge=1, le=8)

    # --- Chat provider -----------------------------------------------------
    #: Which chat provider the brain uses: "openai" keeps the OPENAI_* values
    #: (including an OpenAI-compatible base URL such as Gemini's) and
    #: "openrouter" talks to OpenRouter through the same OpenAI-compatible API.
    llm_provider: str = "openai"
    #: OpenRouter key. Read only by the server and never logged.
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    #: Any current free OpenRouter model. "openrouter/free" routes to whichever
    #: free model is live, so the default survives the catalogue rotating;
    #: pin a specific id such as "google/gemma-4-31b-it:free" when needed.
    openrouter_model: str = "openrouter/free"

    # --- Conversation ------------------------------------------------------
    #: How many previous messages are sent as context on each turn.
    chat_context_messages: int = Field(default=20, ge=1, le=100)
    #: Context on a live call. A phone conversation rarely needs 20 turns and
    #: every extra message delays the first spoken sentence.
    call_context_messages: int = Field(default=8, ge=1, le=100)
    #: Model used for spoken turns. Empty means the normal chat model.
    call_llm_model: str = ""
    #: Optional replacement persona. Safety rules are always appended.
    system_prompt: str | None = None

    # --- Voice -------------------------------------------------------------
    openai_stt_model: str = "whisper-1"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "alloy"
    voice_max_upload_mb: int = Field(default=15, ge=1, le=50)
    #: "openai" (Whisper/TTS) or "gemini" (Gemini transcribe + TTS models).
    speech_provider: str = "openai"
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    gemini_stt_model: str = "gemini-3.5-transcribe"
    #: Second transcription model tried when the first is rate limited (429).
    #: Empty keeps the previous behaviour (single model, bounded retries).
    gemini_stt_fallback_model: str = ""
    gemini_voice: str = "Kore"
    gemini_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    #: One transcription request must fail fast: a stalled STT request holds
    #: the whole call. 429 answers arrive quickly, so this is a real ceiling.
    gemini_stt_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    #: Total budget for one utterance (primary model + fallback model).
    gemini_stt_total_seconds: float = Field(default=12.0, gt=0, le=60)
    #: Second transcription backend used only when the primary one fails
    #: (for example Gemini STT HTTP 429). Empty disables it. "whisper" runs
    #: faster-whisper locally on this computer, so it needs no key and no
    #: internet; it is skipped when the package is not installed.
    speech_fallback_provider: str = ""
    #: Local Whisper model size (tiny/base/small/medium). "small" is the
    #: smallest size that still transcribes Uzbek acceptably on a CPU.
    whisper_model: str = "small"
    #: Text-to-speech backend. Empty means "follow SPEECH_PROVIDER".
    #: "edge" uses the keyless Microsoft Edge neural voices (edge-tts).
    tts_provider: str = ""
    #: Used only when the primary TTS fails (for example Gemini HTTP 429).
    #: Empty disables the fallback. "edge" needs no API key.
    tts_fallback_provider: str = "edge"
    edge_tts_voice: str = "uz-UZ-MadinaNeural"
    #: Edge speaking rate. The neural voices are slow at "+0%" for a live call;
    #: +25% is still natural and about 20% shorter.
    edge_tts_rate: str = "+25%"
    edge_tts_pitch: str = "+0Hz"
    edge_tts_volume: str = "+0%"
    #: VoiceLab (Aisha AI) — Uzbek-native voices. "voicelab" in TTS_PROVIDER
    #: uses it; the key is sent as a bearer token and never logged.
    voicelab_api_key: SecretStr | None = None
    voicelab_base_url: str = "https://api.voicelab.uz"
    #: A ``voice_...`` id from GET /v1/voices?language=uz. Gulnoza is the
    #: documented Uzbek default; any Uzbek voice id works.
    voicelab_voice_id: str = "voice_01J9NEUTRAL0000000000000001"
    voicelab_language: str = "uz"
    voicelab_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    #: Language the assistant speaks on a call. Every spoken answer is Uzbek;
    #: the value is also used to check that the chosen TTS voice really is an
    #: Uzbek voice instead of assuming it.
    voice_language: str = "uz"
    #: Language hint sent to the transcription provider. Empty means
    #: "auto-detect", which is what a call needs: the person may speak Russian
    #: or English and still get an Uzbek answer. "uz" forces Uzbek-only STT and
    #: will mis-hear any other language.
    stt_language: str = ""
    tts_timeout_seconds: float = Field(default=45.0, gt=0, le=300)

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
    #: Extra ids that belong to the same owner — a second account or a second
    #: phone. Each one gets the owner's full rights, so list only your own.
    #: Accepts "123,456" or a JSON list.
    telegram_owner_user_ids: list[int] = Field(default_factory=list)
    #: Let other Telegram users chat with Ksenus in a private message. Off by
    #: default on purpose: this is one person's assistant, and opening it to
    #: strangers is a deliberate choice. A guest gets no tools, no owner data
    #: and no call without saying yes first.
    telegram_public_chat_enabled: bool = False
    #: Answer in group chats, but only when the message addresses the account:
    #: an @mention or a reply to one of its messages. It never answers every
    #: group message, so it cannot flood a chat. A group sender who is not the
    #: owner is still a guest: no tools, no owner data.
    telegram_groups_enabled: bool = True
    #: Allow joining a group's voice chat (a Telegram group call). The account
    #: joins muted and listens; speaking in a group call is not enabled yet.
    telegram_group_calls_enabled: bool = True
    #: Extra Telegram user ids allowed to chat while public chat is off.
    #: Accepts "123,456" or a JSON list.
    telegram_allowed_user_ids: list[int] = Field(default_factory=list)
    #: How long one person's "yes, call me" stays valid. Consent lives in this
    #: process only, so a restart asks again instead of reusing an old yes.
    telegram_call_consent_seconds: int = Field(default=600, ge=30, le=86400)
    #: Accept incoming calls without asking. Requires a working media stack.
    telegram_allow_incoming_calls: bool = False
    #: Enables the native call transport. Off until verified on this machine.
    telegram_calls_enabled: bool = False

    # --- Telegram call audio ---------------------------------------------
    #: Accept incoming calls from ANY Telegram user, not only the owner. A guest
    #: caller is answered with guest rules: no tools, no owner data, redacted
    #: replies. Set to false to restore owner-only calls.
    telegram_public_calls_enabled: bool = True
    #: Answer the owner's incoming call automatically. Guest calls are answered
    #: directly whenever TELEGRAM_PUBLIC_CALLS_ENABLED is true.
    telegram_call_auto_answer: bool = True
    telegram_call_max_seconds: int = Field(default=900, ge=30, le=7200)
    telegram_call_ring_timeout_seconds: int = Field(default=45, ge=5, le=180)
    telegram_call_connect_timeout_seconds: int = Field(default=30, ge=5, le=120)
    telegram_call_reconnect_grace_seconds: int = Field(default=20, ge=0, le=120)
    #: Trailing silence that ends one spoken utterance. 700 ms cut slower
    #: speakers mid-sentence, so the default leaves more room to breathe.
    telegram_call_silence_ms: int = Field(default=900, ge=300, le=3000)
    telegram_call_min_speech_ms: int = Field(default=250, ge=100, le=2000)
    #: RMS threshold (int16 scale) above which a 10 ms frame counts as speech.
    telegram_call_energy_threshold: float = Field(default=400.0, ge=0, le=32767)
    #: Consecutive voiced audio needed to interrupt our own speech. A single
    #: loud frame (speaker echo, a click) must not count as an interruption.
    telegram_call_barge_in_ms: int = Field(default=300, ge=60, le=1000)
    #: Barge-in is ignored for this long after playback starts, which is when
    #: the speaker echo is strongest.
    telegram_call_barge_in_grace_ms: int = Field(default=250, ge=0, le=1000)
    #: Energy multiplier applied while we speak, so our own voice coming back
    #: through the caller's microphone is ignored as speech.
    telegram_call_barge_in_threshold_scale: float = Field(default=2.0, ge=1.0, le=5.0)
    #: Hard ceiling for a spoken reply. Models ignore the brevity prompt
    #: sometimes; a 362-character answer is 14 s of audio and makes the queue
    #: fall behind. 0 disables the trim.
    voice_reply_max_chars: int = Field(default=260, ge=0, le=2000)
    #: Largest file the assistant may send to the owner's own Telegram chat.
    telegram_delivery_max_mb: int = Field(default=20, ge=1, le=100)
    #: A queued utterance older than this is stale: it is skipped instead of
    #: pushing an outdated answer into the call, but nothing is lost silently.
    telegram_call_stale_utterance_seconds: float = Field(default=6.0, ge=1.0, le=60.0)
    #: After a detected farewell Ksenus says goodbye and waits this long before
    #: hanging up. Speaking again inside the window cancels the hang-up, so a
    #: misheard word costs a few seconds instead of ending the call.
    telegram_call_goodbye_grace_seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    #: How long one spoken answer may wait for the model. A free model can take
    #: longer than a person tolerates, and dead air is what makes a call feel
    #: broken, so the deadline is short and the call says something instead.
    telegram_call_reply_deadline_seconds: float = Field(default=9.0, ge=2.0, le=60.0)

    # --- Autonomy and security --------------------------------------------
    #: When set, mutating endpoints require this token in the X-API-Token header.
    api_token: SecretStr | None = None
    #: Windows-only global hotkey that opens a one-line prompt window.
    hotkey_enabled: bool = True
    hotkey: str = "ctrl+alt+space"

    # --- Desktop companion ------------------------------------------------ #
    #: The small animated pixel Ksenus on this computer's screen. It appears
    #: together with computer mode; /companion/show puts it on screen at any
    #: time and /companion/hide takes it away.
    companion_enabled: bool = True
    #: Pixel zoom for the sprite (1-4). 2 is the owner's "a little smaller":
    #: the robot keeps sharp pixels and stays a pet, not a poster. Use 3-4 only
    #: on a very high-DPI screen.
    companion_scale: int = Field(default=2, ge=1, le=4)
    #: How long the pet may sit untouched before it falls asleep.
    companion_sleep_seconds: int = Field(default=90, ge=10, le=3600)
    #: Kompyuter rejimida agent o‘z javobini shu kompyuterning dinamigida
    #: aytadi (Windows). O‘chirilsa, agent jim qoladi — soxta ovoz yo‘q.
    companion_voice_enabled: bool = True
    #: Remember what the owner works in, looks at and likes, and use it when
    #: they give a task. Read locally from this computer's windows only.
    owner_profile_enabled: bool = True
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

    @field_validator("voicelab_api_key", "openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("openai_model", "openrouter_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model name must not be empty")
        return normalized

    @field_validator("llm_provider")
    @classmethod
    def _check_llm_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"openai", "openrouter"}:
            raise ValueError("llm_provider must be 'openai' or 'openrouter'")
        return normalized

    @field_validator("openrouter_base_url")
    @classmethod
    def _check_openrouter_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("openrouter_base_url must be an http(s) URL")
        return normalized

    @field_validator("openai_base_url", mode="before")
    @classmethod
    def _blank_base_url_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("speech_provider")
    @classmethod
    def _check_speech_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"openai", "gemini"}:
            raise ValueError("speech_provider must be 'openai' or 'gemini'")
        return normalized

    @field_validator("speech_fallback_provider")
    @classmethod
    def _check_speech_fallback(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"", "openai", "gemini", "whisper"}:
            raise ValueError(
                "speech_fallback_provider must be '', 'openai', 'gemini' or 'whisper'"
            )
        return normalized

    @field_validator("tts_provider", "tts_fallback_provider")
    @classmethod
    def _check_tts_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"", "openai", "gemini", "edge", "voicelab"}:
            raise ValueError(
                "tts_provider/tts_fallback_provider must be '', 'openai', "
                "'gemini', 'edge' or 'voicelab'"
            )
        return normalized

    @property
    def tts_backend(self) -> str:
        """Backend used for synthesis; defaults to the speech provider."""
        return self.tts_provider or self.speech_provider

    @field_validator("voice_language", "stt_language")
    @classmethod
    def _normalise_language(cls, value: str) -> str:
        return (value or "").strip().lower()

    @field_validator("telegram_allowed_user_ids", "telegram_owner_user_ids", mode="before")
    @classmethod
    def _parse_user_id_list(cls, value: object) -> object:
        """Accept ``"123, 456"``, ``"[123,456]"`` or an already parsed list."""
        if value is None or isinstance(value, (list, tuple, set)):
            return value
        if isinstance(value, int):
            # pydantic-settings reads a bare env value as JSON, so a single id
            # arrives as a number rather than as a one-item list.
            return [value]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                return text  # pydantic parses the JSON form itself
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @property
    def owner_user_ids(self) -> frozenset[int]:
        """Every id that counts as the owner: the main one plus the extras."""
        ids = set(self.telegram_owner_user_ids)
        if self.telegram_owner_user_id is not None:
            ids.add(self.telegram_owner_user_id)
        return frozenset(ids)

    @property
    def chat_user_allowed_ids(self) -> frozenset[int]:
        """Every id that may chat: the owner plus the configured guests."""
        ids = set(self.telegram_allowed_user_ids)
        ids.update(self.owner_user_ids)
        return frozenset(ids)

    @property
    def stt_language_hint(self) -> str | None:
        """Language hint for transcription, or ``None`` to auto-detect."""
        return self.stt_language or None

    @property
    def llm_client_timeout(self) -> float:
        """Timeout for chat requests, falling back to the OpenAI setting."""
        return self.llm_timeout_seconds or self.openai_timeout_seconds

    @field_validator("system_prompt")
    @classmethod
    def _normalise_system_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator(
        "openrouter_api_key",
        "telegram_api_hash",
        "api_token",
        "exa_api_key",
        "apy_token",
        mode="before",
    )
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
    def voicelab_key(self) -> str | None:
        """The raw VoiceLab key. Never log or serialise this."""
        return (
            self.voicelab_api_key.get_secret_value()
            if self.voicelab_api_key
            else None
        )

    @property
    def openai_key(self) -> str | None:
        """The raw key for the SDK client. Never log or serialise this."""
        return self.openai_api_key.get_secret_value() if self.openai_api_key else None

    @property
    def openrouter_configured(self) -> bool:
        """True when an OpenRouter key is present. Safe to log."""
        return self.openrouter_api_key is not None

    @property
    def openrouter_key(self) -> str | None:
        """Raw OpenRouter key for the SDK client. Never log or serialise."""
        return (
            self.openrouter_api_key.get_secret_value()
            if self.openrouter_api_key
            else None
        )

    @property
    def exa_configured(self) -> bool:
        """True when a real web-search key is present. Safe to log."""
        return self.exa_api_key is not None

    @property
    def exa_key(self) -> str | None:
        """Raw Exa key for the search client. Never log or serialise this."""
        return self.exa_api_key.get_secret_value() if self.exa_api_key else None

    @property
    def apy_configured(self) -> bool:
        """True when an ApyHub token is present. Safe to log."""
        return self.apy_token is not None

    @property
    def apy_token_value(self) -> str | None:
        """Raw ApyHub token for the image client. Never log or serialise this."""
        return self.apy_token.get_secret_value() if self.apy_token else None

    @property
    def chat_model(self) -> str:
        """The model id the active chat provider will use."""
        if self.llm_provider == "openrouter":
            return self.openrouter_model
        return self.openai_model

    @property
    def llm_configured(self) -> bool:
        """True when the selected chat provider has a key. Safe to log."""
        if self.llm_provider == "openrouter":
            return self.openrouter_configured
        return self.openai_configured

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
