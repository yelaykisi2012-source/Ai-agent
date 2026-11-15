"""Security control tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from config import Settings
from fakes import FakeSpeech, FakeTelegram
from main import create_app, guard
from runtime import AgentRuntime


def _app(settings: Settings):
    runtime = AgentRuntime(settings, telegram=FakeTelegram(), speech=FakeSpeech())
    return create_app(settings, runtime=runtime)


def _request(settings: Settings, host: str, token: str | None = None):
    headers = {}
    if token is not None:
        headers["x-api-token"] = token
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        headers=headers,
        client=SimpleNamespace(host=host),
    )


def test_loopback_without_token_is_allowed(settings: Settings) -> None:
    guard(_request(settings, "127.0.0.1"))


def test_remote_without_token_is_rejected(settings: Settings) -> None:
    with pytest.raises(HTTPException) as excinfo:
        guard(_request(settings, "192.168.1.50"))
    assert excinfo.value.status_code == 403


def test_token_is_required_when_configured(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    protected = Settings(_env_file=None)

    with pytest.raises(HTTPException) as excinfo:
        guard(_request(protected, "127.0.0.1"))
    assert excinfo.value.status_code == 401

    with pytest.raises(HTTPException) as excinfo:
        guard(_request(protected, "127.0.0.1", token="wrong"))
    assert excinfo.value.status_code == 401

    guard(_request(protected, "127.0.0.1", token="s3cret-token"))


def test_mutating_endpoint_requires_token(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    protected = Settings(_env_file=None)
    with TestClient(_app(protected)) as client:
        assert client.post("/control/stop").status_code == 401
        response = client.post(
            "/control/stop", headers={"X-API-Token": "s3cret-token"}
        )
        assert response.status_code == 200
        assert response.json()["paused"] is True


def test_emergency_stop_pauses_scheduler_and_blocks_autonomy(settings: Settings) -> None:
    app = _app(settings)
    with TestClient(app) as client:
        assert client.get("/control/status").json()["paused"] is False

        stopped = client.post("/control/stop").json()
        assert stopped["paused"] is True
        assert app.state.runtime.scheduler.paused is True

        resumed = client.post("/control/resume").json()
        assert resumed["paused"] is False
        assert app.state.runtime.scheduler.paused is False


def test_health_never_leaks_secrets(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    protected = Settings(_env_file=None)
    with TestClient(_app(protected)) as client:
        body = client.get("/health").json()
    assert "openai_configured" in body
    assert "s3cret-token" not in str(body)
    assert "api_token" not in str(body).lower()
