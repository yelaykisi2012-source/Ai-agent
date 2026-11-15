"""Task CRUD.

The manager owns task rows only. When a scheduler is attached, creating or
updating a task with a deadline also books or moves a task-due reminder so the
agent can actually notify the user instead of silently storing a date.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from database import session_scope
from models import VALID_PRIORITIES, VALID_TASK_STATUSES, Task, utcnow

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tasks.reminders import ReminderService

logger = logging.getLogger(__name__)

TASK_DUE_REMINDER_KIND = "task_due"


def as_utc(value: datetime | None) -> datetime | None:
    """Interpret naive timestamps as UTC, normalise aware ones to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TaskManager:
    """Create, list, update and complete tasks."""

    def __init__(self) -> None:
        self._reminders: "ReminderService | None" = None

    def bind_reminders(self, reminders: "ReminderService") -> None:
        """Attach the reminder service used to sync deadlines."""
        self._reminders = reminders

    # -- queries ---------------------------------------------------------- #
    def get(self, task_id: int) -> Task | None:
        with session_scope() as session:
            return session.get(Task, task_id)

    def list(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Task], int, int]:
        """Return tasks, total count and pending count."""
        with session_scope() as session:
            stmt = select(Task)
            if status and status != "all":
                if status not in VALID_TASK_STATUSES:
                    raise ValueError(f"invalid task status: {status!r}")
                stmt = stmt.where(Task.status == status)
            stmt = stmt.order_by(
                Task.status.asc(),
                Task.due_at.is_(None),
                Task.due_at.asc(),
                Task.id.desc(),
            )
            tasks = list(session.scalars(stmt.limit(limit).offset(offset)))
            total = session.scalar(select(func.count()).select_from(Task)) or 0
            pending = (
                session.scalar(
                    select(func.count()).select_from(Task).where(Task.status == "pending")
                )
                or 0
            )
        return tasks, int(total), int(pending)

    def upcoming(self, limit: int = 5) -> list[Task]:
        """Pending tasks with a deadline, soonest first."""
        with session_scope() as session:
            stmt = (
                select(Task)
                .where(Task.status == "pending", Task.due_at.is_not(None))
                .order_by(Task.due_at.asc())
                .limit(limit)
            )
            return list(session.scalars(stmt))

    # -- mutations -------------------------------------------------------- #
    def create(
        self,
        title: str,
        *,
        details: str | None = None,
        priority: str = "normal",
        due_at: datetime | None = None,
        source: str = "chat",
    ) -> Task:
        cleaned = " ".join(title.split())
        if not cleaned:
            raise ValueError("task title must not be empty")
        if priority not in VALID_PRIORITIES:
            raise ValueError(f"invalid priority: {priority!r}")

        now = utcnow()
        with session_scope() as session:
            task = Task(
                title=cleaned[:200],
                details=(details or None),
                priority=priority,
                status="pending",
                due_at=as_utc(due_at),
                source=source,
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            session.flush()
            created = task

        # Scheduling touches SQLite from a second session, so it must happen
        # after this transaction commits or it would block on the write lock.
        self._sync_deadline(created)
        return created

    def update(self, task_id: int, **changes: Any) -> Task | None:
        """Update fields; unknown keys raise ValueError."""
        allowed = {"title", "details", "priority", "due_at", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")

        with session_scope() as session:
            task = session.get(Task, task_id)
            if task is None:
                return None

            if "priority" in changes and changes["priority"] is not None:
                if changes["priority"] not in VALID_PRIORITIES:
                    raise ValueError(f"invalid priority: {changes['priority']!r}")
                task.priority = changes["priority"]
            if "status" in changes and changes["status"] is not None:
                if changes["status"] not in VALID_TASK_STATUSES:
                    raise ValueError(f"invalid status: {changes['status']!r}")
                task.status = changes["status"]
                task.completed_at = utcnow() if changes["status"] == "done" else None
            if "title" in changes and changes["title"] is not None:
                cleaned = " ".join(str(changes["title"]).split())
                if not cleaned:
                    raise ValueError("task title must not be empty")
                task.title = cleaned[:200]
            if "details" in changes:
                task.details = changes["details"] or None
            if "due_at" in changes:
                task.due_at = as_utc(changes["due_at"])

            task.updated_at = utcnow()
            session.flush()
            updated = task

        self._sync_deadline(updated)
        return updated

    def complete(self, task_id: int) -> Task | None:
        """Mark a task done and cancel its pending deadline reminder."""
        return self.update(task_id, status="done")

    def delete(self, task_id: int) -> bool:
        with session_scope() as session:
            task = session.get(Task, task_id)
            if task is None:
                return False
            session.delete(task)
        if self._reminders is not None:
            self._reminders.cancel_for_task(task_id)
        return True

    # -- internals -------------------------------------------------------- #
    def _sync_deadline(self, task: Task) -> None:
        """Book, move or cancel the deadline reminder for a task."""
        if self._reminders is None:
            return
        try:
            if task.status == "pending" and task.due_at is not None:
                self._reminders.sync_task_deadline(
                    task_id=task.id,
                    title=task.title,
                    due_at=task.due_at,
                )
            else:
                self._reminders.cancel_for_task(task.id)
        except Exception:  # noqa: BLE001 - task saving must not depend on scheduling
            logger.exception("Could not sync reminder for task %s", task.id)
