"""Reminder and sleep-check tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from config import Settings
from database import init_database
import memory
from tasks.reminders import ReminderService
from tasks.sleep_monitor import SleepMonitor


class StubScheduler:
    """Deterministic scheduler double."""

    running = True
    paused = False

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []
        self.retries: list[tuple[int, int]] = []
        self.cancelled: list[int] = []

    def schedule_reminder(self, reminder_id: int, remind_at: datetime) -> None:
        self.scheduled.append((reminder_id, remind_at))

    def schedule_retry(self, reminder_id: int, delay_minutes: int) -> None:
        self.retries.append((reminder_id, delay_minutes))

    def cancel_reminder(self, reminder_id: int) -> None:
        self.cancelled.append(reminder_id)


def _service(settings: Settings) -> tuple[ReminderService, StubScheduler, list[str]]:
    init_database(settings)
    scheduler = StubScheduler()
    delivered: list[str] = []

    async def notifier(text: str) -> bool:
        delivered.append(text)
        return True

    service = ReminderService(settings, scheduler, notifier)  # type: ignore[arg-type]
    return service, scheduler, delivered


def test_create_schedules_reminder(settings: Settings) -> None:
    service, scheduler, _ = _service(settings)
    when = datetime.now(timezone.utc) + timedelta(minutes=10)
    reminder = service.create("Drink water", when)

    assert reminder.status == "pending"
    assert scheduler.scheduled and scheduler.scheduled[0][0] == reminder.id


def test_delivery_schedules_retry_until_acknowledged(settings: Settings) -> None:
    service, scheduler, delivered = _service(settings)
    reminder = service.create(
        "Stand up", datetime.now(timezone.utc) - timedelta(minutes=1), schedule=False
    )

    asyncio.run(service.run_due(reminder.id))
    after_first = service.get(reminder.id)
    assert after_first is not None
    assert after_first.attempts == 1
    assert after_first.status == "sent"
    assert delivered and "Stand up" in delivered[0]
    assert scheduler.retries  # retry booked because it is not acknowledged

    assert service.acknowledge(reminder.id) is True
    assert reminder.id in scheduler.cancelled


def test_retry_budget_exhaustion_marks_missed(settings: Settings) -> None:
    init_database(settings)
    scheduler = StubScheduler()

    async def failing_notifier(text: str) -> bool:
        return False

    service = ReminderService(settings, scheduler, failing_notifier)  # type: ignore[arg-type]
    exhausted: list[int] = []
    service.set_exhausted_hook(lambda reminder: exhausted.append(reminder.id))

    reminder = service.create(
        "Take medicine",
        datetime.now(timezone.utc) - timedelta(minutes=1),
        max_attempts=2,
        schedule=False,
    )

    asyncio.run(service.run_due(reminder.id))
    asyncio.run(service.run_due(reminder.id))

    final = service.get(reminder.id)
    assert final is not None
    assert final.status == "missed"
    assert final.attempts == 2
    assert exhausted == [reminder.id]


def test_emergency_stop_defers_reminder(settings: Settings) -> None:
    service, scheduler, delivered = _service(settings)
    reminder = service.create(
        "Deferred", datetime.now(timezone.utc) - timedelta(minutes=1), schedule=False
    )

    memory.set_paused(True)
    asyncio.run(service.run_due(reminder.id))

    assert delivered == []
    assert scheduler.retries and scheduler.retries[0][1] == 5
    stored = service.get(reminder.id)
    assert stored is not None and stored.attempts == 0
    memory.set_paused(False)


def test_restore_registers_pending_reminders(settings: Settings) -> None:
    service, scheduler, _ = _service(settings)
    pending = service.create("Pending", datetime.now(timezone.utc) + timedelta(minutes=5))
    service.create(
        "Cancelled", datetime.now(timezone.utc) + timedelta(minutes=6), schedule=False
    )
    service.cancel(service.list()[0].id)

    scheduler.scheduled.clear()
    restored = service.restore()
    assert restored >= 1
    assert any(reminder_id == pending.id for reminder_id, _ in scheduler.scheduled)


def test_sleep_check_happy_path(settings: Settings) -> None:
    service, scheduler, delivered = _service(settings)
    monitor = SleepMonitor(settings, service)
    service.set_exhausted_hook(monitor.on_reminder_exhausted)

    check = monitor.start(30, label="nap")
    assert check.status == "active"
    assert check.expected_wake_at > check.started_at
    assert scheduler.scheduled

    asyncio.run(service.run_due(scheduler.scheduled[0][0]))
    assert any("Sleep check" in text for text in delivered)

    completed = monitor.record_response()
    assert completed is not None
    assert completed.status == "completed"
    assert completed.responded_at is not None


def test_sleep_check_reports_no_response_not_asleep(settings: Settings) -> None:
    init_database(settings)
    scheduler = StubScheduler()

    async def failing_notifier(text: str) -> bool:
        return False

    service = ReminderService(settings, scheduler, failing_notifier)  # type: ignore[arg-type]
    monitor = SleepMonitor(settings, service)
    service.set_exhausted_hook(monitor.on_reminder_exhausted)

    check = monitor.start(1, label="nap")
    reminder_id = scheduler.scheduled[0][0]

    asyncio.run(service.run_due(reminder_id))
    asyncio.run(service.run_due(reminder_id))

    result = monitor.get(check.id)
    assert result is not None
    assert result.status == "no_response"
    assert "unknown" in (result.notes or "").lower()
    assert result.status != "asleep"
