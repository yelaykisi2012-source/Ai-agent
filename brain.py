"""The agent's brain: OpenAI calls, context assembly and tool routing.

Design notes
------------
* The client is created lazily, so importing the app never needs an API key.
* The SDK's own retries are disabled (``max_retries=0``) because retry policy
  is implemented here: exponential backoff with jitter, honouring
  ``Retry-After``, and only for genuinely retryable failures.
* Provider errors are translated into :class:`BrainError` subclasses carrying a
  safe public message. Raw provider payloads are logged, never returned.
* Tools whose modules do not exist yet are registered with no handler. They are
  therefore never advertised to the model and never executed; if the model
  hallucinates a call anyway, the registry answers "unavailable" instead of
  pretending. Tools marked ``requires_confirmation`` are refused until the
  caller explicitly confirms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from config import Settings
from privacy import redact
from system_prompt import GUEST_RULES, build_system_prompt, voice_rules
from voice.uzbek import language_report

logger = logging.getLogger(__name__)

# Tool outcome statuses.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_CONFIRMATION_REQUIRED = "confirmation_required"
STATUS_ERROR = "error"

#: One corrective request, used only when a spoken answer came back in another
#: language. It is in Uzbek so the model sees the target language, not a
#: description of it.
_UZBEK_REWRITE_REQUEST = (
    "Bu javobni tabiiy, sodda o‘zbek tilida qayta yoz. Faqat o‘zbekcha matnni "
    "qaytar: izoh, tarjima yoki boshqa tildagi so‘z qo‘shma."
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BrainError(Exception):
    """Base class for model-provider failures with a safe public message."""

    status_code: int = 502

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class BrainNotConfigured(BrainError):
    """No usable API key."""

    status_code = 503


class BrainTimeout(BrainError):
    """The provider did not answer within the configured timeout."""

    status_code = 504


class BrainUnavailable(BrainError):
    """Any other provider-side failure."""

    status_code = 502


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolSpec:
    """Declaration of a tool the agent may use.

    ``handler`` is None until the owning module is implemented. Such tools are
    declared (so the API can report them) but never offered to the model.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]] | None = None
    requires_confirmation: bool = False
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.handler is not None

    def as_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolResult:
    """Outcome of an attempted tool call."""

    name: str
    status: str
    detail: str
    requires_confirmation: bool = False
    confirmation_id: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_OK

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool": self.name,
            "status": self.status,
            "detail": self.detail,
            "confirmation_id": self.confirmation_id,
        }


class ToolRegistry:
    """Safe dispatch for tool calls, with confirmation gating.

    ``confirmation_hook`` persists a pending action and returns its id, so the
    user can approve it later from the dashboard or API. It is synchronous by
    design: it only writes a row.
    """

    def __init__(
        self,
        specs: Iterable[ToolSpec] = (),
        confirmation_hook: Callable[[ToolSpec, dict[str, Any], str], str] | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in specs}
        self._confirmation_hook = confirmation_hook

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def available_names(self) -> list[str]:
        return sorted(name for name, spec in self._specs.items() if spec.available)

    def model_tools(self) -> list[dict[str, Any]]:
        """OpenAI tool schemas for implemented tools only."""
        return [spec.as_openai_schema() for spec in self._specs.values() if spec.available]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool = False,
    ) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(name, STATUS_ERROR, f"Unknown tool {name!r}; nothing was executed.")

        if spec.handler is None:
            reason = spec.unavailable_reason or "This capability is not implemented yet."
            return ToolResult(name, STATUS_UNAVAILABLE, f"{reason} Nothing was executed.")

        if spec.requires_confirmation and not confirmed:
            detail = (
                "This action changes the outside world and needs explicit user confirmation. "
                "Nothing was executed."
            )
            confirmation_id = None
            if self._confirmation_hook is not None:
                confirmation_id = self._confirmation_hook(spec, arguments, detail)
            return ToolResult(
                name,
                STATUS_CONFIRMATION_REQUIRED,
                detail,
                requires_confirmation=True,
                confirmation_id=confirmation_id,
            )

        try:
            output = await spec.handler(arguments)
        except Exception:  # noqa: BLE001 - a failing tool must not crash the agent
            logger.exception("Tool %s failed", name)
            return ToolResult(name, STATUS_ERROR, "The tool failed; the action was not completed.")

        detail = str(output) if output not in (None, "") else "Completed successfully."
        return ToolResult(name, STATUS_OK, detail)


def default_tool_registry() -> ToolRegistry:
    """Declare the agent's tools, including those awaiting their module.

    Handlers are added in Phase 4 (tasks), Phase 5 (reminders) and Phase 7
    (phone calls). Until then these tools are visibly unavailable rather than
    silently faked.
    """
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="create_task",
            description="Create a personal task with an optional deadline and priority.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title."},
                    "due_at": {
                        "type": "string",
                        "description": "ISO 8601 deadline, e.g. 2026-11-16T09:00:00+05:00.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Task priority.",
                    },
                    "details": {"type": "string", "description": "Optional notes."},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            unavailable_reason="The task manager is not implemented yet.",
        )
    )
    registry.register(
        ToolSpec(
            name="list_tasks",
            description="List the user's tasks, optionally filtered by status.",
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "done", "all"],
                        "description": "Which tasks to return.",
                    }
                },
                "additionalProperties": False,
            },
            unavailable_reason="The task manager is not implemented yet.",
        )
    )
    registry.register(
        ToolSpec(
            name="complete_task",
            description="Mark an existing task as completed.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "Identifier of the task."},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            unavailable_reason="The task manager is not implemented yet.",
        )
    )
    registry.register(
        ToolSpec(
            name="schedule_reminder",
            description="Schedule a reminder that will notify the user at a specific time.",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What to remind the user about."},
                    "remind_at": {
                        "type": "string",
                        "description": "ISO 8601 timestamp for the reminder.",
                    },
                    "task_id": {
                        "type": "integer",
                        "description": "Optional task this reminder belongs to.",
                    },
                },
                "required": ["message", "remind_at"],
                "additionalProperties": False,
            },
            unavailable_reason="The scheduler is not implemented yet.",
        )
    )
    registry.register(
        ToolSpec(
            name="make_phone_call",
            description="Call the user's phone and speak a message.",
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "What the user should hear when they answer.",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
            requires_confirmation=True,
            unavailable_reason="Phone calls are not implemented yet.",
        )
    )
    return registry


# --------------------------------------------------------------------------- #
# Reply value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BrainReply:
    """A finished assistant reply."""

    text: str
    model: str
    usage: dict[str, int] | None = None
    tool_results: list[ToolResult] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Brain
# --------------------------------------------------------------------------- #
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


class AgentBrain:
    """Turns a message plus recent history into a reply."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        fallback_client: Any | None = None,
        registry: ToolRegistry | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._client_injected = client is not None
        self._fallback_client = fallback_client
        self._fallback_client_injected = fallback_client is not None
        self.tools = registry or default_tool_registry()
        self._system_prompt = system_prompt or build_system_prompt(settings)

    # -- public ----------------------------------------------------------- #
    @property
    def model(self) -> str:
        return self._settings.chat_model

    def set_tool_registry(self, registry: ToolRegistry) -> None:
        """Replace the tool registry (used when services are wired at startup)."""
        self.tools = registry

    def tool_status(self) -> dict[str, bool]:
        """Which tools are actually implemented right now."""
        return {name: self.tools.get(name).available for name in self.tools.names()}  # type: ignore[union-attr]

    async def reply(
        self,
        *,
        history: Sequence[Mapping[str, Any]],
        user_message: str,
        voice_mode: bool = False,
        guest: bool = False,
    ) -> BrainReply:
        """Produce a reply, executing tool calls when the model requests them.

        ``voice_mode`` adds the phone-call brevity rules so a spoken answer
        stays short and natural instead of turning into a document.
        ``guest`` marks a person who is not the owner: they get the privacy
        rules, no tools at all and a redacted answer.
        """
        messages = self._build_messages(history, user_message, voice_mode, guest)
        # Every tool in this registry acts on the owner's own data (tasks,
        # reminders, memory, calls, delivery). A guest may only talk, so the
        # safest possible restriction is to offer no tools to the model.
        tools: list[dict[str, Any]] = [] if guest else self.tools.model_tools()
        tool_results: list[ToolResult] = []
        requested_model = self._turn_model(voice_mode)
        model_name = requested_model
        usage_total: dict[str, int] | None = None
        # A reasoning model can spend a whole small token budget on thinking
        # and answer with finish_reason="length" plus no text. Retry once
        # without the cap instead of turning that into a spoken apology.
        retried_without_cap = False
        # Voice turns are not capped: a truncated call answer helps nobody.
        allow_cap = not voice_mode

        for _round in range(self._settings.openai_max_tool_rounds):
            completion = await self._complete(
                messages, tools, allow_cap=allow_cap, model=requested_model
            )
            served_model = getattr(completion, "model", None)
            if served_model and served_model != requested_model:
                # openrouter/free routes to different models; make it visible.
                logger.info(
                    "Chat model routed: requested=%s served=%s",
                    requested_model,
                    served_model,
                )
            model_name = getattr(completion, "model", None) or model_name
            usage_total = _merge_usage(usage_total, getattr(completion, "usage", None))

            choices = getattr(completion, "choices", None) or []
            if not choices:
                raise BrainUnavailable("The model returned no answer. Please try again.")
            message = choices[0].message

            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                messages.append(_assistant_tool_message(message, tool_calls))
                for call in tool_calls:
                    name = call.function.name
                    arguments = _parse_arguments(call.function.arguments)
                    if arguments is None:
                        result = ToolResult(
                            name,
                            STATUS_ERROR,
                            "The model produced invalid tool arguments; nothing was executed.",
                        )
                    else:
                        result = await self.tools.execute(name, arguments)
                    tool_results.append(result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result.to_payload(), ensure_ascii=False),
                        }
                    )
                continue

            text = (message.content or "").strip()
            finish_reason = getattr(choices[0], "finish_reason", None)
            if not text and not tool_calls:
                if finish_reason == "length" and not retried_without_cap:
                    retried_without_cap = True
                    allow_cap = False
                    logger.warning(
                        "Model hit the token cap with no text (model=%s finish_reason=%s); "
                        "retrying once without max_tokens",
                        model_name,
                        finish_reason,
                    )
                    continue
                # Never invent a reply here: the caller decides what to say.
                logger.warning(
                    "Model returned no text: model=%s provider=%s finish_reason=%s "
                    "choices=%d tool_calls=%d",
                    model_name,
                    self._settings.llm_provider,
                    finish_reason,
                    len(choices),
                    len(tool_calls),
                )
                raise BrainUnavailable(
                    "The model returned an empty reply. Try again or change the model."
                )
            if finish_reason == "length":
                logger.warning(
                    "Reply was truncated at the token cap (model=%s, %d chars); "
                    "raise LLM_MAX_TOKENS if this repeats",
                    model_name,
                    len(text),
                )
            if guest:
                # Last line of defence: a guest answer never carries a key, a
                # path or the owner's contact details, even if the model
                # repeated something from its own context.
                text, _changed = redact(text, extra_secrets=self._owner_secrets())
            if voice_mode:
                text = await self._enforce_spoken_language(messages, text)
            return BrainReply(
                text=text,
                model=model_name,
                usage=usage_total,
                tool_results=tool_results,
            )

        raise BrainUnavailable(
            "The model kept requesting tools without producing an answer. Try rephrasing."
        )

    # -- internals -------------------------------------------------------- #
    def _build_messages(
        self,
        history: Sequence[Mapping[str, Any]],
        user_message: str,
        voice_mode: bool = False,
        guest: bool = False,
    ) -> list[dict[str, Any]]:
        history = list(history)[-self._settings.chat_context_messages :]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_message(voice_mode, guest)},
        ]
        for turn in history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _system_message(self, voice_mode: bool = False, guest: bool = False) -> str:
        now = datetime.now(self._settings.tzinfo)
        stamp = now.strftime("%A, %d %B %Y, %H:%M")
        prompt = self._system_prompt
        if voice_mode:
            prompt = f"{prompt}\n\n{voice_rules()}"
        if guest:
            prompt = f"{prompt}\n\n{GUEST_RULES}"
        return (
            f"{prompt}\n\n"
            f"Runtime context: the person's local time is {stamp} ({self._settings.timezone})."
        )

    def _owner_secrets(self) -> tuple[str, ...]:
        """Owner details that must never appear in a guest's answer."""
        settings = self._settings
        values: list[str] = []
        if settings.telegram_phone:
            values.append(settings.telegram_phone)
        if settings.telegram_owner_user_id is not None:
            values.append(str(settings.telegram_owner_user_id))
        return tuple(values)

    async def _enforce_spoken_language(
        self, messages: list[dict[str, Any]], text: str
    ) -> str:
        """Keep a spoken answer in Uzbek, whatever language the person used.

        The call rules ask for Uzbek, but a small model can still answer in the
        language it just heard. One rewrite request fixes that. If the rewrite
        is not Uzbek either, the turn is dropped instead of spoken: Russian or
        English through an Uzbek voice is worse than a short silence, and the
        reason stays in the log.
        """
        report = language_report(text)
        if report.ok:
            return text
        logger.warning(
            "Voice reply was not Uzbek (%s); asking once for an Uzbek rewrite",
            report.detail,
        )
        retry_messages = [
            *messages,
            {"role": "assistant", "content": text},
            {"role": "user", "content": _UZBEK_REWRITE_REQUEST},
        ]
        # No tools and no token cap: this is a rewrite of an answer that
        # already exists, not a new turn.
        completion = await self._complete(retry_messages, [], allow_cap=False)
        choices = getattr(completion, "choices", None) or []
        rewritten = (choices[0].message.content or "").strip() if choices else ""
        if not rewritten:
            logger.warning("Uzbek rewrite returned no text; nothing is spoken")
            raise BrainUnavailable("The answer could not be rewritten in Uzbek.")
        still = language_report(rewritten)
        if not still.ok:
            logger.warning(
                "Rewrite is still not Uzbek (%s); nothing is spoken", still.detail
            )
            raise BrainUnavailable("The answer could not be rewritten in Uzbek.")
        logger.info("Voice reply rewritten into Uzbek (%d chars)", len(rewritten))
        return rewritten

    def _ensure_client(self) -> Any:
        """Build the client for the configured chat provider.

        Both providers speak the OpenAI Chat Completions protocol, so only the
        key and base URL change; the OPENAI_* path (including Gemini's
        OpenAI-compatible endpoint) stays exactly as before.
        """
        if self._client is not None:
            return self._client
        if self._settings.llm_provider == "openrouter":
            key = self._settings.openrouter_key
            if not key:
                raise BrainNotConfigured(
                    "No OpenRouter API key is configured. Set OPENROUTER_API_KEY in .env "
                    "or set LLM_PROVIDER=openai."
                )
            self._client = AsyncOpenAI(
                api_key=key,
                base_url=self._settings.openrouter_base_url,
                timeout=self._settings.llm_client_timeout,
                max_retries=0,
            )
            return self._client
        key = self._settings.openai_key
        if not key:
            raise BrainNotConfigured(
                "No OpenAI API key is configured. Set OPENAI_API_KEY in .env and restart."
            )
        self._client = AsyncOpenAI(
            api_key=key,
            base_url=self._settings.openai_base_url,
            timeout=self._settings.llm_client_timeout,
            max_retries=0,
        )
        return self._client

    def _ensure_fallback_client(self) -> Any | None:
        """The OPENAI_* provider used when OpenRouter fails, if it is set up.

        Fallback is intentionally one-directional: choosing ``openai`` keeps
        the previous behaviour (no second network hop), and it only happens
        when an OPENAI_API_KEY exists to talk to.
        """
        if self._fallback_client_injected:
            return self._fallback_client
        if self._fallback_client is not None:
            return self._fallback_client
        if self._settings.llm_provider != "openrouter":
            return None
        if not self._settings.openai_configured:
            return None
        self._fallback_client = AsyncOpenAI(
            api_key=self._settings.openai_key,
            base_url=self._settings.openai_base_url,
            timeout=self._settings.llm_client_timeout,
            max_retries=0,
        )
        return self._fallback_client

    def _turn_model(self, voice: bool) -> str:
        """Model for this turn: a call may use a faster one than chat."""
        if voice:
            override = self._settings.call_llm_model.strip()
            if override:
                return override
        return self.model

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        allow_cap: bool = True,
        model: str | None = None,
    ) -> Any:
        provider = self._settings.llm_provider
        kwargs = self._request_kwargs(model or self.model, messages, tools, allow_cap=allow_cap)
        try:
            return await self._complete_with(self._ensure_client(), kwargs)
        except BrainError as exc:
            fallback = self._ensure_fallback_client()
            if fallback is None:
                raise
            logger.warning(
                "Chat provider %s failed (%s); falling back to the OPENAI_* provider",
                provider,
                type(exc).__name__,
            )
            fallback_kwargs = self._request_kwargs(
                self._settings.openai_model, messages, tools, allow_cap=allow_cap
            )
            return await self._complete_with(fallback, fallback_kwargs)

    def _request_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        allow_cap: bool = True,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if allow_cap and self._settings.llm_max_tokens > 0:
            # Caps generation time on slow free models and keeps replies short.
            kwargs["max_tokens"] = self._settings.llm_max_tokens
        return kwargs

    async def _complete_with(self, client: Any, kwargs: dict[str, Any]) -> Any:
        attempts = self._settings.openai_max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await client.chat.completions.create(**kwargs)
            except _RETRYABLE as exc:
                error = self._translate_error(exc)
                if attempt >= attempts:
                    logger.error(
                        "OpenAI call failed after %d attempt(s): %s",
                        attempt,
                        type(exc).__name__,
                        exc_info=True,
                    )
                    raise error from exc
                delay = self._backoff_delay(attempt, exc)
                logger.warning(
                    "OpenAI call failed (%s); retrying in %.2fs [%d/%d]",
                    type(exc).__name__,
                    delay,
                    attempt,
                    attempts - 1,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            except (AuthenticationError, BadRequestError, APIStatusError) as exc:
                logger.error("OpenAI rejected the request: %s", type(exc).__name__, exc_info=True)
                raise self._translate_error(exc) from exc

        # Unreachable: the loop either returns or raises.
        raise BrainUnavailable("The model request failed. Please try again.")

    def _backoff_delay(self, attempt: int, exc: Exception) -> float:
        base = self._settings.openai_retry_base_delay
        cap = self._settings.openai_retry_max_delay
        delay = 0.0 if base <= 0 else min(cap, base * (2 ** (attempt - 1)))

        retry_after = _retry_after_seconds(exc)
        if retry_after is not None:
            delay = max(delay, min(retry_after, cap))

        if delay > 0:
            delay += random.uniform(0, delay * 0.25)
        return round(delay, 3)

    def _translate_error(self, exc: Exception) -> BrainError:
        """Map a provider exception to a safe, user-facing error."""
        if isinstance(exc, APITimeoutError):
            return BrainTimeout(
                "The language model took too long to answer. Please try again."
            )
        if isinstance(exc, AuthenticationError):
            return BrainNotConfigured(
                "OpenAI rejected the API key. Check OPENAI_API_KEY in .env."
            )
        if isinstance(exc, BadRequestError):
            return BrainUnavailable(
                "OpenAI rejected the request. Check OPENAI_MODEL and try again."
            )
        if isinstance(exc, RateLimitError):
            return BrainUnavailable(
                "The OpenAI rate limit or quota was reached. Try again in a moment."
            )
        if isinstance(exc, APIConnectionError):
            return BrainUnavailable(
                "Could not reach OpenAI. Check the internet connection and try again."
            )
        if isinstance(exc, APIStatusError):
            return BrainUnavailable(
                f"OpenAI returned an error (HTTP {exc.status_code}). Please try again later."
            )
        return BrainUnavailable("The language model failed. Please try again.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_arguments(raw: str | None) -> dict[str, Any] | None:
    """Parse tool arguments, returning None when they are unusable."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Model returned non-JSON tool arguments")
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _assistant_tool_message(message: Any, tool_calls: Sequence[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ],
    }


def _usage_dict(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    try:
        return {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        }
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def _merge_usage(
    total: dict[str, int] | None,
    usage: Any,
) -> dict[str, int] | None:
    current = _usage_dict(usage)
    if current is None:
        return total
    if total is None:
        return current
    return {key: total.get(key, 0) + current.get(key, 0) for key in current}


def _retry_after_seconds(exc: Exception) -> float | None:
    """Read a ``Retry-After`` header if the provider sent one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after")
    except Exception:  # pragma: no cover - defensive
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
