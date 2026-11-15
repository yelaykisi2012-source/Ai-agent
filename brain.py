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
from system_prompt import build_system_prompt

logger = logging.getLogger(__name__)

# Tool outcome statuses.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_CONFIRMATION_REQUIRED = "confirmation_required"
STATUS_ERROR = "error"


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
        registry: ToolRegistry | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._client_injected = client is not None
        self.tools = registry or default_tool_registry()
        self._system_prompt = system_prompt or build_system_prompt(settings)

    # -- public ----------------------------------------------------------- #
    @property
    def model(self) -> str:
        return self._settings.openai_model

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
    ) -> BrainReply:
        """Produce a reply, executing tool calls when the model requests them."""
        messages = self._build_messages(history, user_message)
        tools = self.tools.model_tools()
        tool_results: list[ToolResult] = []
        model_name = self._settings.openai_model
        usage_total: dict[str, int] | None = None

        for _round in range(self._settings.openai_max_tool_rounds):
            completion = await self._complete(messages, tools)
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
            if not text:
                raise BrainUnavailable(
                    "The model returned an empty reply. Try again or change OPENAI_MODEL."
                )
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
    ) -> list[dict[str, Any]]:
        history = list(history)[-self._settings.chat_context_messages :]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_message()},
        ]
        for turn in history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _system_message(self) -> str:
        now = datetime.now(self._settings.tzinfo)
        stamp = now.strftime("%A, %d %B %Y, %H:%M")
        return (
            f"{self._system_prompt}\n\n"
            f"Runtime context: the person's local time is {stamp} ({self._settings.timezone})."
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = self._settings.openai_key
        if not key:
            raise BrainNotConfigured(
                "No OpenAI API key is configured. Set OPENAI_API_KEY in .env and restart."
            )
        self._client = AsyncOpenAI(
            api_key=key,
            timeout=self._settings.openai_timeout_seconds,
            max_retries=0,
        )
        return self._client

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self._settings.openai_model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

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
