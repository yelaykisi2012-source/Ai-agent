"""Reminder execution.

A reminder is not "done" when a message is sent; it is done when the user
acknowledges it or the retry budget is exhausted. Every attempt is recorded so
the agent can report honestly what happened.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import select

from config import Settings
from database import record_event, session_scope
from models import Reminder, utcnow
from tasks.scheduler import AgentScheduler, as_utc

logger = logging.getLogger(__name__)

Notifier = Callable[..., Awaitable[bool]]
ExhaustedHook = Callable[[Reminder], None]
#: Writes the message to send for a reminder, or None to stay silent.
Composer = Callable[[Reminder], Awaitable[str | None]]

ACTIVE_STATUSES = ("pending", "sent")
#: Repeating reminders: the distance between two occurrences.
REPEAT_RULES: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}


class ReminderService:
    """Create, schedule, fire and acknowledge reminders."""

    def __init__(
        self,
        settings: Settings,
        scheduler: AgentScheduler,
        notifier: Notifier,
        composer: Composer | None = None,
    ) -> None:
        self._settings = settings
        self._scheduler = scheduler
        self._notifier = notifier
        self._on_exhausted: ExhaustedHook | None = None
        self._composer = composer

    def set_exhausted_hook(self, hook: ExhaustedHook) -> None:
        """Called once when a reminder runs out of attempts."""
        self._on_exhausted = hook

    def set_composer(self, composer: Composer | None) -> None:
        """Attach the agent that decides what a due reminder should say."""
        self._composer = composer

    # -- creation --------------------------------------------------------- #
    def create(
        self,
        message: str,
        remind_at: datetime,
        *,
        task_id: int | None = None,
        kind: str = "reminder",
        max_attempts: int | None = None,
        interval_minutes: int | None = None,
        sleep_check_id: int | None = None,
        schedule: bool = True,
        repeat: str = "",
    ) -> Reminder:
        cleaned = message.strip()
        if not cleaned:
            raise ValueError("reminder message must not be empty")
        repeat = (repeat or "").strip().lower()
        if repeat and repeat not in REPEAT_RULES:
            raise ValueError("repeat must be '', 'daily' or 'weekly'")

        with session_scope() as session:
            reminder = Reminder(
                task_id=task_id,
                sleep_check_id=sleep_check_id,
                kind=kind,
                message=cleaned,
                remind_at=as_utc(remind_at),
                status="pending",
                attempts=0,
                max_attempts=max_attempts or self._settings.reminder_max_attempts,
                interval_minutes=interval_minutes or self._settings.reminder_retry_minutes,
                repeat=repeat,
                created_at=utcnow(),
            )
            session.add(reminder)
            session.flush()
            reminder_id = reminder.id
            scheduled_for = reminder.remind_at

        if schedule and self._scheduler.running:
            self._scheduler.schedule_reminder(reminder_id, scheduled_for)
        else:
            logger.warning("Reminder %s saved but not scheduled (scheduler idle)", reminder_id)

        record_event(
            source="reminder",
            message=f"Reminder #{reminder_id} created",
            details=f"kind={kind}, at={scheduled_for.isoformat()}, repeat={repeat or 'no'}",
        )
        created = self.get(reminder_id)
        if created is None:  # pragma: no cover - insert just succeeded
            raise RuntimeError("reminder disappeared after creation")
        return created

    def sync_task_deadline(self, task_id: int, title: str, due_at: datetime) -> Reminder:
        """Create or move the single task-due reminder for a task."""
        with session_scope() as session:
            existing = session.scalars(
                select(Reminder).where(
                    Reminder.task_id == task_id,
                    Reminder.kind == "task_due",
                    Reminder.status.in_(ACTIVE_STATUSES),
                )
            ).first()
            if existing is not None:
                existing.message = f"Task due: {title}"
                existing.remind_at = as_utc(due_at)
                session.flush()
                reminder_id = existing.id
                scheduled_for = existing.remind_at
                created = False
            else:
                reminder_id = 0
                scheduled_for = as_utc(due_at)
                created = True

        if not created:
            if self._scheduler.running:
                self._scheduler.schedule_reminder(reminder_id, scheduled_for)
            reminder = self.get(reminder_id)
            if reminder is None:  # pragma: no cover - row was just read
                raise RuntimeError("reminder disappeared during update")
            return reminder

        return self.create(
            f"Task due: {title}",
            due_at,
            task_id=task_id,
            kind="task_due",
        )

    # -- queries ---------------------------------------------------------- #
    def get(self, reminder_id: int) -> Reminder | None:
        with session_scope() as session:
            return session.get(Reminder, reminder_id)

    def pending(self) -> Sequence[Reminder]:
        with session_scope() as session:
            stmt = (
                select(Reminder)
                .where(Reminder.status == "pending")
                .order_by(Reminder.remind_at.asc())
            )
            return list(session.scalars(stmt))

    def list(self, status: str | None = None, limit: int = 50) -> Sequence[Reminder]:
        with session_scope() as session:
            stmt = select(Reminder)
            if status and status != "all":
                stmt = stmt.where(Reminder.status == status)
            stmt = stmt.order_by(Reminder.remind_at.desc()).limit(limit)
            return list(session.scalars(stmt))

    def active_for_sleep_check(self, sleep_check_id: int) -> Sequence[Reminder]:
        with session_scope() as session:
            stmt = select(Reminder).where(
                Reminder.sleep_check_id == sleep_check_id,
                Reminder.status.in_(ACTIVE_STATUSES),
            )
            return list(session.scalars(stmt))

    # -- execution -------------------------------------------------------- #
    async def run_due(self, reminder_id: int) -> None:
        """Job entry point: notify, then schedule a retry if unanswered."""
        reminder = self.get(reminder_id)
        if reminder is None or reminder.status not in ACTIVE_STATUSES:
            return

        if self._is_paused():
            logger.info("Emergency stop engaged; reminder %s deferred", reminder_id)
            self._scheduler.schedule_retry(reminder_id, 5)
            return

        attempt = reminder.attempts + 1
        if self._composer is not None and reminder.kind in ("reminder", "task_due"):
            # The agent writes the actual message instead of forwarding a
            # canned string. None means it judged the reminder unnecessary.
            text = ""
            try:
                text = (await self._composer(reminder)) or ""
            except Exception:  # noqa: BLE001 - a compose failure must not crash the scheduler
                logger.exception("Reminder composer failed for %s", reminder_id)
            if not text.strip():
                self.acknowledge(reminder_id)
                record_event(
                    source="reminder",
                    message=f"Reminder #{reminder_id} skipped by the agent",
                    details=f"kind={reminder.kind}",
                )
                return
        elif reminder.kind == "sleep_check":
            text = (
                f"Sleep check ({attempt}/{reminder.max_attempts}): "
                f"{reminder.message} Reply when you see this."
            )
        elif reminder.kind == "task_due":
            text = f"Task due: {reminder.message}"
        else:
            text = f"Reminder: {reminder.message}"

        delivered = False
        try:
            delivered = await self._notify(text, reminder_id)
        except Exception:  # noqa: BLE001 - a notifier must never crash the scheduler
            logger.exception("Notifier failed for reminder %s", reminder_id)

        exhausted = False
        with session_scope() as session:
            row = session.get(Reminder, reminder_id)
            if row is None:
                return
            row.attempts = attempt
            row.last_attempt_at = utcnow()
            exhausted = attempt >= row.max_attempts
            if exhausted:
                row.status = "missed"
            elif delivered:
                row.status = "sent"
            session.flush()
            snapshot = row

        record_event(
            source="reminder",
            message=f"Reminder #{reminder_id} attempt {attempt} "
            f"{'delivered' if delivered else 'not delivered'}",
            level="INFO" if delivered else "WARNING",
            details=f"kind={snapshot.kind}",
        )

        if exhausted:
            logger.warning("Reminder %s exhausted after %d attempts", reminder_id, attempt)
            if self._on_exhausted is not None:
                self._on_exhausted(snapshot)
            return

        # Not delivered, or delivered but unacknowledged: try again.
        self._scheduler.schedule_retry(reminder_id, snapshot.interval_minutes)

    async def _notify(self, text: str, reminder_id: int) -> bool:
        """Send one line; a notifier that knows the id gets it as well.

        The messenger keeps the id so it can acknowledge the reminder when the
        window shows it; a plain notifier (Telegram, tests) takes one argument.
        """
        try:
            return await self._notifier(text, reminder_id=reminder_id)
        except TypeError:
            return await self._notifier(text)

    def acknowledge(self, reminder_id: int) -> bool:
        """Mark a reminder acknowledged; stops further retries."""
        with session_scope() as session:
            reminder = session.get(Reminder, reminder_id)
            if reminder is None:
                return False
            reminder.status = "acknowledged"
            reminder.acknowledged_at = utcnow()
        self._scheduler.cancel_reminder(reminder_id)
        self._schedule_next(reminder_id)
        return True

    def _schedule_next(self, reminder_id: int) -> int | None:
        """Create the next occurrence of a repeating reminder.

        The next time is computed from the reminder's own time, not from the
        moment it was acknowledged, so «har kuni 9:00» stays at 9:00. Past
        occurrences (the computer was off for two days) are skipped instead of
        firing a burst of old reminders.
        """
        with session_scope() as session:
            row = session.get(Reminder, reminder_id)
            if row is None or not row.repeat or row.repeat not in REPEAT_RULES:
                return None
            step = REPEAT_RULES[row.repeat]
            next_at = as_utc(row.remind_at) + step
            now = utcnow()
            while next_at <= now:
                next_at += step
            message = row.message
            kind = row.kind
            interval = row.interval_minutes
            repeat = row.repeat

        created = self.create(
            message,
            next_at,
            kind=kind,
            interval_minutes=interval,
            repeat=repeat,
        )
        logger.info(
            "Repeating reminder %s -> #%s at %s",
            reminder_id,
            created.id,
            next_at.isoformat(timespec="minutes"),
        )
        return created.id

    def acknowledge_all_pending(self) -> int:
        """Acknowledge every outstanding reminder; returns how many."""
        with session_scope() as session:
            rows = list(
                session.scalars(
                    select(Reminder).where(Reminder.status.in_(ACTIVE_STATUSES))
                )
            )
            now = utcnow()
            for row in rows:
                row.status = "acknowledged"
                row.acknowledged_at = now
            ids = [row.id for row in rows]
        for reminder_id in ids:
            self._scheduler.cancel_reminder(reminder_id)
        return len(ids)

    def cancel(self, reminder_id: int) -> bool:
        with session_scope() as session:
            reminder = session.get(Reminder, reminder_id)
            if reminder is None:
                return False
            reminder.status = "cancelled"
        self._scheduler.cancel_reminder(reminder_id)
        return True

    def cancel_for_task(self, task_id: int) -> int:
        with session_scope() as session:
            rows = list(
                session.scalars(
                    select(Reminder).where(
                        Reminder.task_id == task_id,
                        Reminder.status.in_(ACTIVE_STATUSES),
                    )
                )
            )
            for row in rows:
                row.status = "cancelled"
            ids = [row.id for row in rows]
        for reminder_id in ids:
            self._scheduler.cancel_reminder(reminder_id)
        return len(ids)

    def restore(self) -> int:
        """Re-register every pending reminder after a restart.

        Overdue reminders are scheduled for "now" and fire immediately, which
        is how the agent survives a restart without losing notifications.
        """
        rows = self.pending()
        for reminder in rows:
            self._scheduler.schedule_reminder(reminder.id, as_utc(reminder.remind_at))
        if rows:
            logger.info("Restored %d pending reminder(s) from the database", len(rows))
        return len(rows)

    # -- internals -------------------------------------------------------- #
    @staticmethod
    def _is_paused() -> bool:
        from memory import is_paused

        return is_paused()


__all__ = ["ReminderService", "Notifier", "as_utc"]
