"""HTTP layer tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from config import Settings
from main import create_app


def test_root_reports_version(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == settings.app_name
    assert body["health"] == "/health"
    assert "dashboard" in body


def test_health_reports_database_ok(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["timezone"] == settings.timezone
    assert body["events_logged"] >= 1  # startup event was written


def test_missing_database_returns_503() -> None:
    """Without a lifespan run there is no engine, so health must not claim ok."""
    import database

    database.dispose_database()
    app = create_app(Settings(_env_file=None))
    with TestClient(app) as client:
        # TestClient always runs lifespan, so simulate a broken engine instead.
        database.dispose_database()
        response = client.get("/health")
    assert response.status_code == 503
