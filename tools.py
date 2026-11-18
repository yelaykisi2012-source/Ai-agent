"""Tool implementations exposed to the model.

Every handler performs a real operation or reports that it cannot. Tools that
change the outside world are marked ``requires_confirmation``; the registry
then stores a pending action and returns its id for the user to approve, so a
model can never dial a phone on its own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import memory
from brain import ToolRegistry, ToolSpec
from config import Settings
from tasks.manager import TaskManager
from tasks.reminders import ReminderService
from tasks.sleep_monitor import SleepMonitor
from telegram.calls import CallManager

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Services a tool handler may use."""

    settings: Settings
    tasks: TaskManager
    reminders: ReminderService
    sleep: SleepMonitor
    calls: CallManager
    #: Owner-only Telegram delivery (text, code, tables, files).
    delivery: Any = None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    if not value:
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "no deadline"
    return value.isoformat(timespec="minutes")


async def _create_task(context: ToolContext, arguments: dict[str, Any]) -> str:
    title = str(arguments.get("title") or "").strip()
    if not title:
        return "I need a task title to create a task. Nothing was saved."
    due_at = _parse_datetime(arguments.get("due_at"))
    task = context.tasks.create(
        title=title,
        details=arguments.get("details"),
        priority=str(arguments.get("priority") or "normal"),
        due_at=due_at,
        source="chat",
    )
    return f"Task #{task.id} created: {task.title} ({_format_dt(due_at)})."


async def _list_tasks(context: ToolContext, arguments: dict[str, Any]) -> str:
    status = str(arguments.get("status") or "pending")
    tasks, total, pending = context.tasks.list(status=status, limit=15)
    if not tasks:
        return f"No {status} tasks. ({pending} pending in total.)"
    lines = [
        f"#{task.id} [{task.priority}] {task.title} — {_format_dt(task.due_at)} ({task.status})"
        for task in tasks
    ]
    return f"{len(tasks)} of {total} task(s):\n" + "\n".join(lines)


async def _complete_task(context: ToolContext, arguments: dict[str, Any]) -> str:
    task_id = arguments.get("task_id")
    if not isinstance(task_id, int):
        return "I need a numeric task id to complete a task."
    task = context.tasks.complete(task_id)
    if task is None:
        return f"Task #{task_id} does not exist; nothing changed."
    return f"Task #{task.id} marked done: {task.title}."


async def _update_task(context: ToolContext, arguments: dict[str, Any]) -> str:
    task_id = arguments.get("task_id")
    if not isinstance(task_id, int):
        return "I need a numeric task id to update a task."
    changes: dict[str, Any] = {}
    if "title" in arguments:
        changes["title"] = arguments["title"]
    if "details" in arguments:
        changes["details"] = arguments["details"]
    if "priority" in arguments:
        changes["priority"] = arguments["priority"]
    if "status" in arguments:
        changes["status"] = arguments["status"]
    if "due_at" in arguments:
        changes["due_at"] = _parse_datetime(arguments.get("due_at"))
    if not changes:
        return "No changes were supplied; the task is unchanged."
    task = context.tasks.update(task_id, **changes)
    if task is None:
        return f"Task #{task_id} does not exist; nothing changed."
    return f"Task #{task.id} updated ({_format_dt(task.due_at)})."


async def _schedule_reminder(context: ToolContext, arguments: dict[str, Any]) -> str:
    message = str(arguments.get("message") or "").strip()
    remind_at = _parse_datetime(arguments.get("remind_at"))
    if not message or remind_at is None:
        return "A reminder needs a message and an ISO-8601 remind_at time. Nothing was scheduled."
    reminder = context.reminders.create(message, remind_at)
    return f"Reminder #{reminder.id} scheduled for {remind_at.isoformat(timespec='minutes')}."


async def _cancel_reminder(context: ToolContext, arguments: dict[str, Any]) -> str:
    reminder_id = arguments.get("reminder_id")
    if not isinstance(reminder_id, int):
        return "I need a numeric reminder id to cancel a reminder."
    if not context.reminders.cancel(reminder_id):
        return f"Reminder #{reminder_id} does not exist; nothing changed."
    return f"Reminder #{reminder_id} cancelled."


async def _schedule_call(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Create a reminder that dials the owner instead of sending text."""
    now = datetime.now(context.settings.tzinfo)
    raw_minutes = arguments.get("minutes")
    minutes = raw_minutes if isinstance(raw_minutes, int) and 1 <= raw_minutes <= 720 else None
    at = _parse_datetime(arguments.get("at"))
    if minutes is not None:
        when = now + timedelta(minutes=minutes)
    elif at is not None:
        when = at
    else:
        return (
            "A scheduled call needs minutes (1-720) or an ISO-8601 at time. "
            "Nothing was scheduled."
        )
    if when <= now:
        return "That time is already in the past; nothing was scheduled."
    reason = str(arguments.get("reason") or "").strip()
    label = reason or (f"Qo‘ng‘iroq ({minutes} daqiqadan keyin)" if minutes else "Qo‘ng‘iroq")
    reminder = context.reminders.create(label, when, kind="call")
    return (
        f"Call scheduled for {when.isoformat(timespec='minutes')} "
        f"(reminder #{reminder.id})."
    )


async def _save_memory(context: ToolContext, arguments: dict[str, Any]) -> str:
    content = str(arguments.get("content") or "").strip()
    if not content:
        return "There was nothing to remember."
    entry = memory.add_memory(
        content, category=str(arguments.get("category") or "general"), source="tool"
    )
    return f"Memory #{entry.id} saved in category '{entry.category}'."


async def _retrieve_memory(context: ToolContext, arguments: dict[str, Any]) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        entries, total = memory.list_memories(limit=10)
        if not entries:
            return "I have no stored memories yet."
        return f"{total} stored memory item(s):\n" + "\n".join(
            f"#{entry.id} [{entry.category}] {entry.content}" for entry in entries
        )
    entries = memory.search_memories(query, limit=10)
    if not entries:
        return f"No stored memory matches '{query}'."
    return "\n".join(f"#{entry.id} [{entry.category}] {entry.content}" for entry in entries)


async def _initiate_call(context: ToolContext, arguments: dict[str, Any]) -> str:
    reason = str(arguments.get("reason") or "a reminder").strip()
    started, detail = await context.calls.place(reason)
    return detail


async def _start_sleep_check(context: ToolContext, arguments: dict[str, Any]) -> str:
    raw_minutes = arguments.get("minutes", 30)
    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError):
        return "How many minutes should I wait before checking on you?"
    if minutes < 1:
        return "The sleep check needs at least one minute."
    check = context.sleep.start(minutes, label=str(arguments.get("label") or "sleep"))
    return (
        f"Sleep check #{check.id} started. I will contact you at "
        f"{check.expected_wake_at.isoformat(timespec='minutes')} and retry if you do not reply. "
        "I will not claim you were asleep."
    )


async def _answer_call(context: ToolContext, arguments: dict[str, Any]) -> str:
    _answered, detail = await context.calls.answer()
    return detail


async def _end_call(context: ToolContext, arguments: dict[str, Any]) -> str:
    _ended, detail = await context.calls.hangup("owner_hangup")
    return detail


async def _send_to_telegram(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Send prepared material to the owner's own private chat only."""
    delivery = context.delivery
    if delivery is None:
        return "Sending to Telegram is not available right now; nothing was sent."
    rows = arguments.get("rows")
    if rows is not None and (
        not isinstance(rows, list) or not all(isinstance(row, list) for row in rows)
    ):
        return "Not sent: rows must be a list of rows."
    result = await delivery.send(
        kind=str(arguments.get("kind") or "text"),
        content=str(arguments.get("content") or ""),
        rows=rows,
        filename=arguments.get("filename"),
        table_format=str(arguments.get("table_format") or "xlsx"),
        caption=arguments.get("caption"),
    )
    return result.detail


def _spec(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler,
    *,
    requires_confirmation: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        requires_confirmation=requires_confirmation,
    )


def build_tool_registry(context: ToolContext) -> ToolRegistry:
    """Create the live registry with every implemented tool."""
    specs = [
        _spec(
            "create_task",
            "Create a personal task with an optional deadline and priority.",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due_at": {"type": "string", "description": "ISO 8601 deadline."},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "details": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            lambda args: _create_task(context, args),
        ),
        _spec(
            "list_tasks",
            "List the user's tasks, optionally filtered by status.",
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pending", "done", "all"]}
                },
                "additionalProperties": False,
            },
            lambda args: _list_tasks(context, args),
        ),
        _spec(
            "complete_task",
            "Mark an existing task as completed.",
            {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
            lambda args: _complete_task(context, args),
        ),
        _spec(
            "update_task",
            "Update a task's title, details, priority, deadline or status.",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "status": {"type": "string", "enum": ["pending", "done", "cancelled"]},
                    "due_at": {"type": "string"},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            lambda args: _update_task(context, args),
        ),
        _spec(
            "schedule_reminder",
            "Schedule a reminder that notifies the user at a specific time.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "remind_at": {"type": "string", "description": "ISO 8601 timestamp."},
                    "task_id": {"type": "integer"},
                },
                "required": ["message", "remind_at"],
                "additionalProperties": False,
            },
            lambda args: _schedule_reminder(context, args),
        ),
        _spec(
            "cancel_reminder",
            "Cancel a scheduled reminder.",
            {
                "type": "object",
                "properties": {"reminder_id": {"type": "integer"}},
                "required": ["reminder_id"],
                "additionalProperties": False,
            },
            lambda args: _cancel_reminder(context, args),
        ),
        _spec(
            "schedule_call",
            "Schedule a Telegram call to the owner at a later time. Use it when the "
            "owner says something like \"menga 3 daqiqadan keyin qo‘ng‘iroq qil\" or "
            "\"keyinroq qo‘ng‘iroq qil\". No confirmation is needed: the owner asked "
            "for the call themselves.",
            {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 720,
                        "description": "Minutes from now.",
                    },
                    "at": {"type": "string", "description": "ISO 8601 timestamp."},
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
            lambda args: _schedule_call(context, args),
        ),
        _spec(
            "save_memory",
            "Save a durable preference, instruction or personal detail.",
            {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["content"],
                "additionalProperties": False,
            },
            lambda args: _save_memory(context, args),
        ),
        _spec(
            "retrieve_memory",
            "Search stored memories, or list them when no query is given.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            lambda args: _retrieve_memory(context, args),
        ),
        _spec(
            "start_sleep_check",
            "Schedule a check-in after N minutes and retry if the user does not reply.",
            {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "minimum": 1},
                    "label": {"type": "string"},
                },
                "required": ["minutes"],
                "additionalProperties": False,
            },
            lambda args: _start_sleep_check(context, args),
        ),
        _spec(
            "initiate_telegram_call",
            "Call the user on Telegram and talk to them.",
            {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
                "additionalProperties": False,
            },
            lambda args: _initiate_call(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "answer_telegram_call",
            "Answer an incoming Telegram call.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: _answer_call(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "end_telegram_call",
            "End the current Telegram call.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: _end_call(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "send_to_my_telegram",
            "Send text, code, a table or a file to the owner's OWN private Telegram "
            "chat. Use it whenever the user asks to send something to their Telegram "
            "(\"menga Telegramdan yubor\", \"lichkamga tashla\"). There is no option to "
            "send to another person: only the owner's chat exists.",
            {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["text", "code", "table", "file"],
                        "description": "text = chat message, code = .py file, "
                        "table = spreadsheet, file = text file with a filename.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete material itself (the whole code, "
                        "the whole text), not a summary and not the user's request. "
                        "Never include API keys, passwords or tokens.",
                    },
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": "Table rows (first row is the header).",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Human-readable file name without a path.",
                    },
                    "table_format": {"type": "string", "enum": ["xlsx", "csv"]},
                    "caption": {"type": "string"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
            lambda args: _send_to_telegram(context, args),
        ),
    ]

    def confirmation_hook(spec: ToolSpec, arguments: dict[str, Any], detail: str) -> str:
        summary = f"{spec.name} " + json.dumps(arguments, ensure_ascii=False)
        return memory.create_pending_action(spec.name, arguments, summary)

    return ToolRegistry(specs, confirmation_hook=confirmation_hook)
