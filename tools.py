"""Tool implementations exposed to the model.

Every handler performs a real operation or reports that it cannot. Tools that
change the outside world are marked ``requires_confirmation``; the registry
then stores a pending action and returns its id for the user to approve, so a
model can never dial a phone on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import memory
from brain import ToolRegistry, ToolSpec
from config import Settings
from tasks.manager import TaskManager
from tasks.reminders import ReminderService
from tasks.sleep_monitor import SleepMonitor
import youtube
from apps import AppError, launch as launch_app
from computer import ComputerError
from integrations.apyhub import ApyHubError
from local_files import LocalFilesError
from telegram.calls import CallManager
from web import WebError

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
    #: Sandboxed access to this computer's project folder (None = disabled).
    files: Any = None
    #: The agent's own run log.
    runs: Any = None
    #: Shell and file control on this computer (None = disabled).
    computer: Any = None
    #: Public web search and page reading (None = disabled).
    web: Any = None
    #: ApyHub background removal (None = disabled).
    images: Any = None
    #: The Telegram user account, for joining and listing groups.
    telegram: Any = None
    #: Voice chats inside Telegram groups (None = disabled).
    group_calls: Any = None
    #: Kompyuter rejimi: Telegram call + window on this computer.
    computer_mode: Any = None
    #: Real mouse/keyboard on this screen plus the animated cursor (None = off).
    gui: Any = None


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
    repeat = str(arguments.get("repeat") or "").strip().lower()
    try:
        reminder = context.reminders.create(message, remind_at, repeat=repeat)
    except ValueError as exc:
        return f"Reminder not scheduled: {exc}."
    when = remind_at.isoformat(timespec="minutes")
    if reminder.repeat:
        return (
            f"Reminder #{reminder.id} scheduled for {when}, repeating "
            f"{reminder.repeat}."
        )
    return f"Reminder #{reminder.id} scheduled for {when}."


async def _cancel_reminder(context: ToolContext, arguments: dict[str, Any]) -> str:
    reminder_id = arguments.get("reminder_id")
    if not isinstance(reminder_id, int):
        return "I need a numeric reminder id to cancel a reminder."
    if not context.reminders.cancel(reminder_id):
        return f"Reminder #{reminder_id} does not exist; nothing changed."
    return f"Reminder #{reminder_id} cancelled."


async def _list_runs(context: ToolContext, arguments: dict[str, Any]) -> str:
    runs = getattr(context, "runs", None)
    if runs is None:
        return "The run log is not available."
    limit = arguments.get("limit")
    limit = limit if isinstance(limit, int) and 1 <= limit <= 50 else 10
    rows = await asyncio.to_thread(runs.list, limit)
    if not rows:
        return "No agent runs recorded yet."
    lines = []
    for row in rows:
        when = row.started_at.strftime("%Y-%m-%d %H:%M") if row.started_at else "?"
        seconds = f" {row.duration_ms / 1000:.1f}s" if row.duration_ms is not None else ""
        line = f"#{row.id} {when} {row.trigger}/{row.channel} {row.status}{seconds} tools={row.tools}"
        if row.error:
            line += f" error={row.error}"
        lines.append(line)
    return "Recent agent runs:\n" + "\n".join(lines)


async def _read_file(context: ToolContext, arguments: dict[str, Any]) -> str:
    files = getattr(context, "files", None)
    if files is None:
        return "Local file access is disabled (LOCAL_FILES_ENABLED=false). Nothing was read."
    path = str(arguments.get("path") or "").strip()
    if not path:
        return "I need a file path. Nothing was read."
    try:
        content, size = files.read(path)
    except LocalFilesError as exc:
        return f"Cannot read {path}: {exc}."
    return f"{path} ({size} bytes):\n\n{content}"


async def _write_file(context: ToolContext, arguments: dict[str, Any]) -> str:
    files = getattr(context, "files", None)
    if files is None:
        return "Local file access is disabled (LOCAL_FILES_ENABLED=false). Nothing was written."
    path = str(arguments.get("path") or "").strip()
    content = arguments.get("content")
    if not path or content is None:
        return "I need both path and content. Nothing was written."
    try:
        written, size = files.write(path, str(content))
    except LocalFilesError as exc:
        return f"Cannot write {path}: {exc}."
    return f"Wrote {size} bytes to {written}."


async def _list_files(context: ToolContext, arguments: dict[str, Any]) -> str:
    files = getattr(context, "files", None)
    if files is None:
        return "Local file access is disabled (LOCAL_FILES_ENABLED=false). Nothing was listed."
    path = str(arguments.get("path") or ".").strip()
    try:
        entries = files.list(path)
    except LocalFilesError as exc:
        return f"Cannot list {path}: {exc}."
    if not entries:
        return f"{path} is empty."
    return f"{path}:\n" + "\n".join(entries)


async def _run_command(context: ToolContext, arguments: dict[str, Any]) -> str:
    computer = getattr(context, "computer", None)
    if computer is None:
        return (
            "Running commands on this computer is disabled "
            "(COMPUTER_ENABLED=false). Nothing was run."
        )
    command = str(arguments.get("command") or "").strip()
    if not command:
        return "I need a command to run. Nothing was run."
    raw_timeout = arguments.get("timeout")
    try:
        result = await computer.run(
            command,
            cwd=str(arguments.get("cwd") or "."),
            timeout=raw_timeout if isinstance(raw_timeout, int) else None,
        )
    except ComputerError as exc:
        return f"Refused: {exc}. Nothing was run."
    return result.describe()


async def _edit_file(context: ToolContext, arguments: dict[str, Any]) -> str:
    computer = getattr(context, "computer", None)
    if computer is None:
        return "Editing files is disabled on this computer. Nothing changed."
    path = str(arguments.get("path") or "").strip()
    old = arguments.get("old")
    new = arguments.get("new")
    if not path or old is None or new is None:
        return "I need path, old and new to edit a file. Nothing changed."
    try:
        detail, _hits = computer.edit(
            path,
            str(old),
            str(new),
            replace_all=bool(arguments.get("replace_all")),
        )
    except ComputerError as exc:
        return f"Cannot edit {path}: {exc}."
    return detail


async def _move_file(context: ToolContext, arguments: dict[str, Any]) -> str:
    computer = getattr(context, "computer", None)
    if computer is None:
        return "Moving files is disabled on this computer. Nothing changed."
    source = str(arguments.get("source") or "").strip()
    destination = str(arguments.get("destination") or "").strip()
    if not source or not destination:
        return "I need both source and destination. Nothing changed."
    try:
        return computer.move(source, destination)
    except ComputerError as exc:
        return f"Cannot move {source}: {exc}."


async def _delete_file(context: ToolContext, arguments: dict[str, Any]) -> str:
    computer = getattr(context, "computer", None)
    if computer is None:
        return "Deleting files is disabled on this computer. Nothing changed."
    path = str(arguments.get("path") or "").strip()
    if not path:
        return "I need a file path to delete. Nothing changed."
    try:
        return computer.delete(path)
    except ComputerError as exc:
        return f"Cannot delete {path}: {exc}."


async def _system_info(context: ToolContext, arguments: dict[str, Any]) -> str:
    computer = getattr(context, "computer", None)
    if computer is None:
        return "Computer access is disabled. I cannot describe this machine."
    return computer.info()


async def _web_search(context: ToolContext, arguments: dict[str, Any]) -> str:
    web = getattr(context, "web", None)
    if web is None:
        return "Web access is disabled (WEB_ENABLED=false). Nothing was searched."
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "I need a search query. Nothing was searched."
    raw_limit = arguments.get("max_results")
    try:
        return await web.search(query, raw_limit if isinstance(raw_limit, int) else None)
    except WebError as exc:
        return f"Web search failed: {exc}. Nothing was found."


async def _web_fetch(context: ToolContext, arguments: dict[str, Any]) -> str:
    web = getattr(context, "web", None)
    if web is None:
        return "Web access is disabled (WEB_ENABLED=false). Nothing was read."
    url = str(arguments.get("url") or "").strip()
    if not url:
        return "I need a page URL. Nothing was read."
    try:
        return await web.fetch(url)
    except WebError as exc:
        return f"Cannot read {url}: {exc}"


async def _remove_background(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Cut the background out of one local image; the original is kept as is."""
    images = getattr(context, "images", None)
    files = getattr(context, "files", None)
    if images is None:
        return "Background removal is not available right now; nothing was changed."
    if files is None:
        return "Local file access is disabled (LOCAL_FILES_ENABLED=false). Nothing was changed."
    path = str(arguments.get("path") or "").strip()
    if not path:
        return "I need the image path. Nothing was changed."
    try:
        source = files.resolve(path)
    except LocalFilesError as exc:
        return f"Cannot use {path}: {exc}."
    if not source.is_file():
        return f"Cannot use {path}: no such file."
    try:
        data = source.read_bytes()
    except OSError as exc:
        return f"Cannot read {path}: {exc.strerror or exc}. Nothing was changed."

    try:
        cutout = await images.remove_background(data)
    except ApyHubError as exc:
        return (
            f"Background was not removed from {path}: {exc}. "
            "The original file is unchanged."
        )

    destination = source.with_name(f"{source.stem}-no-bg.png")
    counter = 2
    while destination.exists():
        destination = source.with_name(f"{source.stem}-no-bg-{counter}.png")
        counter += 1
    try:
        destination.write_bytes(cutout.png)
    except OSError as exc:
        return (
            f"The cutout arrived but could not be saved ({exc.strerror or exc}); "
            "the original file is unchanged."
        )

    saved = destination.relative_to(files.root).as_posix()
    checked = (
        f"{cutout.transparent_pixels} fully transparent pixel(s) checked"
        if cutout.transparent_pixels is not None
        else "an alpha channel"
    )
    return (
        f"Background removed: {saved} ({cutout.width}x{cutout.height} PNG, {checked}). "
        f"The original {source.name} is unchanged. The edges are ApyHub's cutout, "
        "not a promise of perfect edges."
    )


async def _start_computer_mode(
    context: ToolContext, arguments: dict[str, Any]
) -> str:
    manager = getattr(context, "computer_mode", None)
    if manager is None:
        return "Kompyuter rejimi mavjud emas."
    reason = str(arguments.get("reason") or "kompyuter rejimi").strip()
    raw_goal = arguments.get("goal")
    goal = str(raw_goal).strip() if raw_goal else None
    _started, detail = await manager.start(reason, goal)
    return detail


async def _stop_computer_mode(
    context: ToolContext, arguments: dict[str, Any]
) -> str:
    manager = getattr(context, "computer_mode", None)
    if manager is None:
        return "Kompyuter rejimi mavjud emas."
    _stopped, detail = await manager.stop()
    return detail


async def _join_group_call(context: ToolContext, arguments: dict[str, Any]) -> str:
    manager = getattr(context, "group_calls", None)
    if manager is None:
        return "Group calls are not available; nothing was joined."
    target = str(arguments.get("chat") or "").strip()
    if not target:
        return "I need the group's @username or name. Nothing was joined."
    _joined, detail = await manager.join(target)
    return detail


async def _leave_group_call(context: ToolContext, arguments: dict[str, Any]) -> str:
    manager = getattr(context, "group_calls", None)
    if manager is None:
        return "Group calls are not available."
    _left, detail = await manager.leave()
    return detail


async def _join_group(context: ToolContext, arguments: dict[str, Any]) -> str:
    telegram = getattr(context, "telegram", None)
    if telegram is None or not getattr(telegram, "ready", False):
        return "Telegram is not connected, so no group was joined."
    target = str(arguments.get("link") or "").strip()
    if not target:
        return "I need a group @username or a t.me invite link. Nothing was joined."
    try:
        _joined, detail = await telegram.join_group(target)
    except Exception:  # noqa: BLE001 - a failed join must not crash the agent
        logger.exception("join_group failed")
        return "Could not join that group; nothing changed."
    return detail


async def _list_groups(context: ToolContext, arguments: dict[str, Any]) -> str:
    telegram = getattr(context, "telegram", None)
    if telegram is None or not getattr(telegram, "ready", False):
        return "Telegram is not connected, so there are no groups to list."
    raw_limit = arguments.get("limit")
    limit = raw_limit if isinstance(raw_limit, int) and 1 <= raw_limit <= 50 else 20
    rows = await telegram.list_groups(limit)
    if not rows:
        return "This account is not in any group or channel."
    return "Groups and channels:\n" + "\n".join(f"- {row}" for row in rows)


async def _read_channel(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Read a public channel's newest posts, with their links, without joining."""
    telegram = getattr(context, "telegram", None)
    if telegram is None or not getattr(telegram, "ready", False):
        return "Telegram is not connected, so the channel was not read."
    channel = str(arguments.get("channel") or "").strip()
    if not channel:
        return "Qaysi kanalni o‘qishni aytmadingiz. Hech narsa o‘qilmadi."
    raw_limit = arguments.get("limit")
    limit = raw_limit if isinstance(raw_limit, int) and 1 <= raw_limit <= 100 else 20
    try:
        rows, error = await telegram.channel_posts(channel, limit)
    except Exception:  # noqa: BLE001 - a broken read must not crash the turn
        logger.exception("read_channel failed")
        return "Kanalni o‘qib bo‘lmadi. Hech narsa o‘qilmadi."
    if error:
        return error
    return f"{channel} — oxirgi {len(rows)} post:\n" + "\n".join(rows)


async def _search_channel(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Search a public channel's history without joining it."""
    telegram = getattr(context, "telegram", None)
    if telegram is None or not getattr(telegram, "ready", False):
        return "Telegram is not connected, so the channel was not searched."
    channel = str(arguments.get("channel") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not channel or not query:
        return "Kanal va qidiruv so‘zi kerak. Hech narsa qidirilmadi."
    raw_limit = arguments.get("limit")
    limit = raw_limit if isinstance(raw_limit, int) and 20 <= raw_limit <= 2000 else 300
    try:
        rows, error = await telegram.search_channel(channel, query, limit=limit)
    except Exception:  # noqa: BLE001 - a broken search must not crash the turn
        logger.exception("search_channel failed")
        return "Kanalda qidirib bo‘lmadi. Hech narsa topilmadi."
    if error:
        return error
    return f"{channel} — «{query}» bo‘yicha {len(rows)} post:\n" + "\n".join(rows)


async def _summarize_link(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Read one link: title, source and a short description."""
    url = str(arguments.get("url") or "").strip()
    if not url:
        return "Havola berilmadi; hech narsa o‘qilmadi."
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "Faqat http(s) havola o‘qiladi. Hech narsa o‘qilmadi."
    try:
        import links

        return await links.summarize(url)
    except Exception:  # noqa: BLE001 - an unreachable link is a normal answer
        logger.exception("summarize_link failed")
        return "Havolani o‘qib bo‘lmadi. Hech narsa o‘qilmadi."


async def _open_url(context: ToolContext, arguments: dict[str, Any]) -> str:
    computer = getattr(context, "computer", None)
    if computer is None:
        return "Opening a browser is disabled on this computer. Nothing was opened."
    url = str(arguments.get("url") or "").strip()
    if not url:
        return "I need a URL to open. Nothing was opened."
    try:
        return await computer.open_url(url)
    except ComputerError as exc:
        return f"Cannot open {url}: {exc}"


async def _open_app(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Start any program on this computer by the name the owner uses."""
    settings = getattr(context, "settings", None)
    if not getattr(settings, "computer_enabled", True):
        return (
            "Kompyuter boshqaruvi o‘chirilgan (COMPUTER_ENABLED=false). "
            "Hech narsa ochilmadi."
        )
    app_name = str(arguments.get("app") or "").strip()
    if not app_name:
        return "Qaysi ilovani ochishni aytmadingiz. Hech narsa ochilmadi."
    extra = str(arguments.get("arguments") or "").strip()
    try:
        return await asyncio.to_thread(launch_app, app_name, extra)
    except AppError as exc:
        return f"«{app_name}» ochilmadi: {exc}. Hech narsa ochilmadi."


async def _play_youtube(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Find the real video for a song and open it playing."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "Qaysi qo‘shiqni qo‘yishni aytmadingiz. Hech narsa qo‘yilmadi."
    wanted = str(arguments.get("title") or "").strip() or query
    try:
        videos = await asyncio.to_thread(youtube.search, query, 6)
        video = youtube.pick(videos, wanted)
        detail = await asyncio.to_thread(youtube.play, video)
    except youtube.YouTubeError as exc:
        return f"Qo‘yib bo‘lmadi: {exc}. Hech narsa qo‘yilmadi."
    extra = f"{video.channel}, {video.duration}" if video.duration else video.channel
    return f"{detail}: {video.url}" + (f" ({extra})" if extra else "")


async def _screen_info(context: ToolContext, arguments: dict[str, Any]) -> str:
    gui = getattr(context, "gui", None)
    if gui is None:
        return "Ekran boshqaruvi o‘chirilgan (GUI_CONTROL_ENABLED=false)."
    return gui.describe()


async def _control_screen(context: ToolContext, arguments: dict[str, Any]) -> str:
    """Do one approved on-screen plan with the real pointer."""
    gui = getattr(context, "gui", None)
    if gui is None:
        return (
            "Ekran boshqaruvi o‘chirilgan (GUI_CONTROL_ENABLED=false). "
            "Hech narsa bajarilmadi."
        )
    goal = str(arguments.get("goal") or "").strip()
    return await gui.run(goal, arguments.get("steps"))


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
    requires_confirmation: bool | Any = False,
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
    computer = getattr(context, "computer", None)

    def command_needs_confirmation(arguments: dict[str, Any]) -> bool:
        """Read-only commands run at once; everything else asks the owner."""
        if computer is None:
            return False
        return computer.needs_confirmation(str(arguments.get("command") or ""))

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
            "Schedule a reminder that notifies the user at a specific time. Set "
            "repeat when the owner wants it again: «har kuni 9:00 da» is daily, "
            "«har dushanba» is weekly. Each occurrence fires at the same clock "
            "time and the next one is created as soon as the current one is "
            "acknowledged.",
            {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "remind_at": {"type": "string", "description": "ISO 8601 timestamp."},
                    "task_id": {"type": "integer"},
                    "repeat": {
                        "type": "string",
                        "enum": ["", "daily", "weekly"],
                        "description": "Empty for a one-off reminder.",
                    },
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
        _spec(
            "list_runs",
            "List the agent's own recent runs: what triggered them, on which "
            "channel, whether they succeeded and how long they took. Use it when "
            "the owner asks what you have done or why something happened.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
                "additionalProperties": False,
            },
            lambda args: _list_runs(context, args),
        ),
        _spec(
            "read_file",
            "Read a UTF-8 text file inside this computer's project folder. Use it "
            "when the owner asks about a file's contents or code. Credential "
            "files and paths outside the project are refused.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the project root, e.g. README.md.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda args: _read_file(context, args),
        ),
        _spec(
            "write_file",
            "Write a UTF-8 text file inside the project folder. Use it only when the "
            "owner explicitly asks to create or change a file. Never put secrets in "
            "the content.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root."},
                    "content": {"type": "string", "description": "The complete file content."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            lambda args: _write_file(context, args),
        ),
        _spec(
            "list_files",
            "List the files and folders in a directory of the project.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Folder relative to the project root; defaults to the root.",
                    }
                },
                "additionalProperties": False,
            },
            lambda args: _list_files(context, args),
        ),
        _spec(
            "run_command",
            "Run one shell command in this computer's project folder (Windows "
            "cmd). Read-only commands such as dir, type or git status run at "
            "once; anything that changes something (install, build, move, "
            "delete) is held for the owner's confirmation first, so the result "
            "may say it is waiting for approval. Report the real exit code and "
            "output, and read an error before trying the same command again.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "One shell command, e.g. \"python -m pytest -q\".",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Folder inside the project; defaults to the root.",
                    },
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            lambda args: _run_command(context, args),
            requires_confirmation=command_needs_confirmation,
        ),
        _spec(
            "edit_file",
            "Replace an exact piece of text in a file inside the project folder. "
            "Prefer this over write_file when only a few lines change, so the "
            "rest of the file is never rewritten by accident.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root."},
                    "old": {"type": "string", "description": "The exact existing text to replace."},
                    "new": {"type": "string", "description": "The replacement text."},
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence instead of requiring a unique one.",
                    },
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
            lambda args: _edit_file(context, args),
        ),
        _spec(
            "move_file",
            "Move or rename one file inside the project folder. Never overwrites "
            "an existing file.",
            {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
            lambda args: _move_file(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "delete_file",
            "Delete exactly one file inside the project folder. Folders are "
            "never deleted. Use it only when the owner asks for the deletion.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda args: _delete_file(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "system_info",
            "Describe this computer: operating system, Python version, project "
            "folder, free disk space and whether commands are enabled.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: _system_info(context, args),
        ),
        _spec(
            "web_search",
            "Search the public web. Use it for facts that change with time "
            "(prices, news, versions, schedules) instead of guessing. It returns "
            "titles, URLs and snippets; read one with web_fetch before quoting it.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            lambda args: _web_search(context, args),
        ),
        _spec(
            "web_fetch",
            "Read one public web page as plain text (title, final URL and the "
            "page text). Localhost, private addresses and file paths are refused.",
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Public http(s) page, e.g. https://example.com/news",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            lambda args: _web_fetch(context, args),
        ),
        _spec(
            "remove_background",
            "Remove the background from an image file inside this project folder and "
            "save a transparent PNG next to it (ApyHub). Use it when the owner asks "
            "to cut a subject out of a photo, e.g. «rasmning fonini olib tashla». "
            "The original file is never changed or deleted.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Image path inside the project folder.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda args: _remove_background(context, args),
        ),
        _spec(
            "open_url",
            "Open a public web page in this computer's default browser, for the "
            "owner to look at themselves. Nothing is read or clicked for them.",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http(s) page."}
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            lambda args: _open_url(context, args),
        ),
        _spec(
            "open_app",
            "Open any program on this computer by name: VS Code (\"code\"), "
            "Telegram, Chrome, Edge, Notepad, Calculator, Explorer, Word, Excel, "
            "Spotify and the rest. Pass one argument for a folder or file: "
            "open_app(app=\"code\", arguments=\"src\") shows that folder in VS "
            "Code. Use it whenever the owner asks to open, start or show a "
            "program; write the code with write_file/edit_file first, then open "
            "VS Code on the folder so the owner sees the real file.",
            {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": (
                            "Program name or path, e.g. code, telegram, chrome, "
                            "notepad, calc, explorer."
                        ),
                    },
                    "arguments": {
                        "type": "string",
                        "description": (
                            "Optional single argument: a folder or file to open "
                            "with it."
                        ),
                    },
                },
                "required": ["app"],
                "additionalProperties": False,
            },
            lambda args: _open_app(context, args),
        ),
        _spec(
            "play_youtube",
            "Search YouTube and PLAY the song or video the owner asked for: it "
            "finds the real video, opens its watch page and starts playback with "
            "sound. Use this for «… qo‘shig‘ini qo‘y», «… qo‘yib ber», «… ni "
            "qo‘y» — never just open the search results page and leave the owner "
            "one click short.",
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to search for, e.g. \"Yulduz Usmonova Onamga "
                            "aytmang\"."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Optional exact title hint when the owner named the "
                            "song; used to pick the right video."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            lambda args: _play_youtube(context, args),
        ),
        _spec(
            "screen_info",
            "Describe this computer's screen: size in pixels, where the mouse "
            "pointer is now, and the title of the window in front. Read-only; "
            "use it before planning clicks so coordinates are real.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: _screen_info(context, args),
        ),
        _spec(
            "control_screen",
            "Control this computer's screen with the real mouse and keyboard while "
            "an animated cursor shows every move to the owner. Give one short "
            "plan of steps and it runs at once. Use action=app to start any "
            "program (code, telegram, notepad, calc). Prefer "
            "keyboard steps over pixel guesses: e.g. ctrl+l then type a URL, "
            "key \"/\" inside a site, enter to submit. Examples of keys: enter, "
            "esc, tab, space, ctrl+l, ctrl+t, ctrl+w, pagedown, f5. Click steps "
            "need x and y from screen_info; a click without them presses at the "
            "current pointer. To play a song use play_youtube (it opens the real "
            "video playing) and do not leave the owner on a results page. Report "
            "the real step results, never a step that did not run.",
            {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "What this plan does, in one short Uzbek sentence; it "
                            "is shown on the animated cursor."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": [
                                        "open",
                                        "app",
                                        "move",
                                        "click",
                                        "double_click",
                                        "right_click",
                                        "type",
                                        "key",
                                        "scroll",
                                        "wait",
                                    ],
                                },
                                "x": {
                                    "type": "integer",
                                    "description": "Screen x in pixels.",
                                },
                                "y": {
                                    "type": "integer",
                                    "description": "Screen y in pixels.",
                                },
                                "url": {
                                    "type": "string",
                                    "description": "Public http(s) page for action=open.",
                                },
                                "app": {
                                    "type": "string",
                                    "description": (
                                        "Program name or path for action=app, e.g. "
                                        "\"code\", \"telegram\", \"notepad\", "
                                        "\"chrome\", \"calc\"."
                                    ),
                                },
                                "args": {
                                    "type": "string",
                                    "description": (
                                        "One argument for action=app, e.g. the folder "
                                        "to open in VS Code."
                                    ),
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Text to type for action=type.",
                                },
                                "key": {
                                    "type": "string",
                                    "description": (
                                        "Key or combo for action=key, e.g. enter, "
                                        "ctrl+l, ctrl+t, escape."
                                    ),
                                },
                                "amount": {
                                    "type": "integer",
                                    "minimum": -10,
                                    "maximum": 10,
                                    "description": (
                                        "Wheel notches for action=scroll; "
                                        "negative scrolls down."
                                    ),
                                },
                                "seconds": {
                                    "type": "number",
                                    "description": "Wait time for action=wait.",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
            lambda args: _control_screen(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "list_groups",
            "List the Telegram groups and channels this account is already in "
            "(titles, @usernames and ids). Use it before joining so you do not "
            "join a group twice.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
                "additionalProperties": False,
            },
            lambda args: _list_groups(context, args),
        ),
        _spec(
            "read_channel",
            "Read the newest posts of a public Telegram channel or group by its "
            "@username or t.me/username link, WITHOUT joining it. Each line has "
            "the post's own t.me link, its media kind (video, photo…) and a "
            "snippet. Use it whenever the owner asks what a channel says or asks "
            "for a video link from it: joining is not needed to read.",
            {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "@username, t.me/username or t.me/<username>.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "How many of the newest posts to read.",
                    },
                },
                "required": ["channel"],
                "additionalProperties": False,
            },
            lambda args: _read_channel(context, args),
        ),
        _spec(
            "search_channel",
            "Search a public Telegram channel's history by words, WITHOUT joining "
            "it. Use it when the newest posts do not contain what the owner asked "
            "for («shu kanaldan korea haqidagi videoni top»); read_channel only "
            "sees the newest posts.",
            {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "@username or t.me/username.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Words to look for in the posts and their links.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 20,
                        "maximum": 2000,
                        "description": "How many recent posts to scan (default 300).",
                    },
                },
                "required": ["channel", "query"],
                "additionalProperties": False,
            },
            lambda args: _search_channel(context, args),
        ),
        _spec(
            "summarize_link",
            "Read one link the owner sent and report what it is: a YouTube "
            "video's title and channel, or a page's title, author and "
            "description. Use it when a message is just a link or asks «bu nima "
            "uchun?» / «shu videoni ko‘rib ber». It reports the page's real "
            "metadata and never invents the content.",
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full http(s) link, e.g. https://youtu.be/abc123.",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            lambda args: _summarize_link(context, args),
        ),
        _spec(
            "join_group",
            "Join a Telegram group from a t.me invite link or an @username. It "
            "runs straight away; after joining, the agent answers there only "
            "when mentioned or replied to. To merely read a public channel, use "
            "read_channel instead — no joining is needed.",
            {
                "type": "object",
                "properties": {
                    "link": {
                        "type": "string",
                        "description": "@username, t.me/+invite, t.me/joinchat/... "
                        "or t.me/username.",
                    }
                },
                "required": ["link"],
                "additionalProperties": False,
            },
            lambda args: _join_group(context, args),
        ),
        _spec(
            "join_group_call",
            "Join the voice chat (group call) of a group this account is already "
            "in. The account joins muted and listens; it does not speak in the "
            "group call. The owner must confirm first.",
            {
                "type": "object",
                "properties": {
                    "chat": {
                        "type": "string",
                        "description": "@username, t.me/username or the group's name.",
                    }
                },
                "required": ["chat"],
                "additionalProperties": False,
            },
            lambda args: _join_group_call(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "start_computer_mode",
            "Enter computer mode: put only the small animated agent on this "
            "computer's screen, where it answers out loud through the "
            "computer's own speakers while it controls the computer. No "
            "Telegram call is placed and no extra window opens. Use it when "
            "the owner says things like «kompyuterda ishlaydigan modelga o‘t» "
            "or «kompyuter rejimini yoq». The owner must confirm first.",
            {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why computer mode is being started.",
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "What the owner wants done in computer mode, in one "
                            "short sentence. It is shown as the goal in the window."
                        ),
                    },
                },
                "additionalProperties": False,
            },
            lambda args: _start_computer_mode(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "stop_computer_mode",
            "Leave computer mode: end the Telegram call and close the window on "
            "the computer.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: _stop_computer_mode(context, args),
            requires_confirmation=True,
        ),
        _spec(
            "leave_group_call",
            "Leave the group voice chat the account is currently in.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args: _leave_group_call(context, args),
            requires_confirmation=True,
        ),
    ]

    def confirmation_hook(spec: ToolSpec, arguments: dict[str, Any], detail: str) -> str:
        summary = f"{spec.name} " + json.dumps(arguments, ensure_ascii=False)
        return memory.create_pending_action(spec.name, arguments, summary)

    registry = ToolRegistry(specs, confirmation_hook=confirmation_hook)
    # One permission at the start is the owner's answer for the whole task.
    settings = getattr(context, "settings", None)
    registry.auto_approve = bool(
        getattr(settings, "owner_auto_approve", False)
    )
    # Reading and joining do not reach a person, so the owner's single yes at
    # the start of the task is enough. Only starting a real call to someone
    # still asks for its own "ha", because the other side has to pick up.
    registry.auto_approve_exempt = {"initiate_telegram_call"}
    return registry
