"""Sleep check workflow.

The agent schedules a check, asks the user to confirm they are awake, retries
a bounded number of times, and records one of exactly two conclusions:
"completed" (the user replied) or "no_response" (they did not). It never
records or claims "asleep" — silence is not evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from config import Settings
from database import record_event, session_scope
from models import Reminder, SleepCheck, utcnow
from tasks.reminders import ReminderService
from tasks.scheduler import as_utc

logger = logging.getLogger(__name__)

SLEEP_KIND = "sleep_check"


class SleepMonitor:
    """Start sleep checks and resolve their outcomes from user replies."""

    def __init__(self, settings: Settings, reminders: ReminderService) -> None:
        self._settings = settings
        self._reminders = reminders

    def start(self, minutes: int, label: str = "sleep") -> SleepCheck:
        """Schedule a check after ``minutes`` and return the record."""
        if minutes < 1:
            raise ValueError("minutes must be at least 1")

        now = utcnow()
        expected = now + timedelta(minutes=minutes)
        with session_scope() as session:
            check = SleepCheck(
                started_at=now,
                expected_wake_at=expected,
                status="active",
                attempts=0,
                notes=f"Requested check after {minutes} minute(s) ({label}).",
            )
            session.add(check)
            session.flush()
            check_id = check.id

        self._reminders.create(
            f"You asked me to check on you after {minutes} minutes.",
            expected,
            kind=SLEEP_KIND,
            sleep_check_id=check_id,
        )
        record_event(
            source="sleep",
            message=f"Sleep check #{check_id} scheduled",
            details=f"expected_wake_at={expected.isoformat()}",
        )
        return self.get(check_id)  # type: ignore[return-value]

    def get(self, check_id: int) -> SleepCheck | None:
        with session_scope() as session:
            return session.get(SleepCheck, check_id)

    def latest_active(self) -> SleepCheck | None:
        with session_scope() as session:
            stmt = (
                select(SleepCheck)
                .where(SleepCheck.status == "active")
                .order_by(SleepCheck.id.desc())
            )
            return session.scalars(stmt).first()

    def record_response(self) -> SleepCheck | None:
        """The user replied: close the active check as completed."""
        active = self.latest_active()
        if active is None:
            return None
        for reminder in self._reminders.active_for_sleep_check(active.id):
            self._reminders.acknowledge(reminder.id)
        with session_scope() as session:
            check = session.get(SleepCheck, active.id)
            if check is None:
                return None
            check.status = "completed"
            check.responded_at = utcnow()
            check.notes = (check.notes or "") + " User responded; sleep state not asserted."
            session.flush()
            result = check
        record_event(
            source="sleep",
            message=f"Sleep check #{active.id} completed",
            details="user responded",
        )
        return result

    def mark_no_response(self, check_id: int, attempts: int) -> None:
        """The retry budget is exhausted; record the honest outcome."""
        with session_scope() as session:
            check = session.get(SleepCheck, check_id)
            if check is None or check.status != "active":
                return
            check.status = "no_response"
            check.attempts = attempts
            check.notes = (
                f"No reply after {attempts} attempt(s). "
                "Sleep state is unknown; no evidence was available."
            )
        record_event(
            source="sleep",
            message=f"Sleep check #{check_id} ended without a response",
            level="WARNING",
            details=f"attempts={attempts}",
        )

    def on_reminder_exhausted(self, reminder: Reminder) -> None:
        """Reminder hook: close the linked check when retries run out."""
        if reminder.kind != SLEEP_KIND or reminder.sleep_check_id is None:
            return
        self.mark_no_response(reminder.sleep_check_id, reminder.attempts or reminder.max_attempts)


__all__ = ["SleepMonitor", "SLEEP_KIND", "as_utc"]
