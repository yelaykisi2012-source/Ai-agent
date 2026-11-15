"""Task manager and task API tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from config import Settings
from database import init_database
from fakes import FakeSpeech, FakeTelegram
from main import create_app
from runtime import AgentRuntime
from tasks.manager import TaskManager


class RecordingReminders:
    """Reminder double that records deadline sync calls."""

    def __init__(self) -> None:
        self.synced: list[tuple[int, datetime]] = []
        self.cancelled: list[int] = []

    def sync_task_deadline(self, task_id: int, title: str, due_at: datetime) -> None:
        self.synced.append((task_id, due_at))

    def cancel_for_task(self, task_id: int) -> None:
        self.cancelled.append(task_id)


def _manager(settings: Settings) -> tuple[TaskManager, RecordingReminders]:
    init_database(settings)
    manager = TaskManager()
    reminders = RecordingReminders()
    manager.bind_reminders(reminders)  # type: ignore[arg-type]
    return manager, reminders


def test_create_and_list_tasks(settings: Settings) -> None:
    manager, _ = _manager(settings)
    task = manager.create("Prepare report", priority="high")
    assert task.id >= 1
    assert task.status == "pending"
    assert task.priority == "high"

    tasks, total, pending = manager.list("all")
    assert total == 1 and pending == 1
    assert tasks[0].title == "Prepare report"


def test_deadline_schedules_and_completion_cancels(settings: Settings) -> None:
    manager, reminders = _manager(settings)
    due = datetime.now(timezone.utc) + timedelta(hours=2)
    task = manager.create("Study", due_at=due)

    assert reminders.synced and reminders.synced[0][0] == task.id

    completed = manager.complete(task.id)
    assert completed is not None
    assert completed.status == "done"
    assert task.id in reminders.cancelled


def test_update_task_validates_fields(settings: Settings) -> None:
    manager, _ = _manager(settings)
    task = manager.create("Old title")

    updated = manager.update(task.id, title="New title", priority="low")
    assert updated is not None
    assert updated.title == "New title"
    assert updated.priority == "low"

    for bad in ({"priority": "urgent"}, {"status": "unknown"}, {"nonsense": 1}):
        try:
            manager.update(task.id, **bad)
        except ValueError:
            pass
        else:  # pragma: no cover - must raise
            raise AssertionError(f"invalid update accepted: {bad}")


def test_delete_task(settings: Settings) -> None:
    manager, _ = _manager(settings)
    task = manager.create("Temporary")
    assert manager.delete(task.id) is True
    assert manager.delete(task.id) is False


def _client(settings: Settings) -> TestClient:
    runtime = AgentRuntime(settings, telegram=FakeTelegram(), speech=FakeSpeech())
    return TestClient(create_app(settings, runtime=runtime))


def test_task_api_round_trip(settings: Settings) -> None:
    with _client(settings) as client:
        created = client.post(
            "/tasks",
            json={"title": "Call the dentist", "priority": "high", "details": "before noon"},
        )
        assert created.status_code == 201
        task = created.json()
        assert task["title"] == "Call the dentist"

        listed = client.get("/tasks").json()
        assert listed["total"] == 1
        assert listed["pending"] == 1

        completed = client.post(f"/tasks/{task['id']}/complete")
        assert completed.status_code == 200
        assert completed.json()["status"] == "done"

        assert client.delete(f"/tasks/{task['id']}").status_code == 204
        assert client.delete(f"/tasks/{task['id']}").status_code == 404


def test_task_api_validation(settings: Settings) -> None:
    with _client(settings) as client:
        assert client.post("/tasks", json={"title": "   "}).status_code == 422
        assert client.patch("/tasks/999", json={"priority": "low"}).status_code == 404
