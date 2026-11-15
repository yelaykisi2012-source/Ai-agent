"""Background scheduler.

APScheduler keeps jobs in memory; the database is the source of truth. On
every startup :class:`~tasks.reminders.ReminderService` re-registers pending
reminders from SQLite, so a restart never loses a reminder and overdue ones
fire immediately.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED
from apscheduler.triggers.date import DateTrigger

from config import Settings

logger = logging.getLogger(__name__)


def as_utc(value: datetime) -> datetime:
    """Interpret naive database timestamps as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class AgentScheduler:
    """Thin, testable wrapper around APScheduler's async scheduler."""

    def __init__(
        self,
        settings: Settings,
        run_reminder: Callable[[int], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._run_reminder = run_reminder
        self._scheduler = AsyncIOScheduler(timezone=settings.tzinfo)
        self._started = False

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if not self._started:
            self._scheduler.start()
            self._started = True
            logger.info("Scheduler started (timezone=%s)", self._settings.timezone)

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info("Scheduler stopped")

    @property
    def running(self) -> bool:
        return self._started and self._scheduler.running

    def pause(self) -> None:
        if self._started:
            self._scheduler.pause()

    def resume(self) -> None:
        if self._started:
            self._scheduler.resume()

    @property
    def paused(self) -> bool:
        return self._scheduler.state == STATE_PAUSED

    # -- jobs ------------------------------------------------------------- #
    def schedule_reminder(self, reminder_id: int, remind_at: datetime) -> None:
        """Schedule (or reschedule) a reminder by id."""
        if not self._started:
            logger.warning(
                "Scheduler idle; reminder %s was not scheduled in-process", reminder_id
            )
            return

        run_date = as_utc(remind_at)
        now = datetime.now(timezone.utc)
        if run_date < now:
            run_date = now

        self._scheduler.add_job(
            self._run_reminder,
            trigger=DateTrigger(run_date=run_date),
            args=[reminder_id],
            id=f"reminder:{reminder_id}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        logger.info("Reminder %s scheduled for %s", reminder_id, run_date.isoformat())

    def schedule_retry(self, reminder_id: int, delay_minutes: int) -> None:
        """Schedule the next notification attempt."""
        self.schedule_reminder(
            reminder_id,
            datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
        )

    def cancel_reminder(self, reminder_id: int) -> None:
        job_id = f"reminder:{reminder_id}"
        try:
            self._scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001 - APScheduler raises JobLookupError
            logger.debug("No scheduled job for reminder %s", reminder_id)

    def job_ids(self) -> list[str]:
        return [job.id for job in self._scheduler.get_jobs()]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in self._scheduler.get_jobs()
        ]
