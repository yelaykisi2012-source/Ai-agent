"""Telegram signaling tests. No network, no credentials."""

from __future__ import annotations

import asyncio
import logging
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from config import Settings
from database import init_database
from fakes import FakeSpeech, FakeTelegram, fake_call_update
from main import create_app
from runtime import AgentRuntime
from telegram.account import TelegramAccount
from telegram.calls import CallManager
from telegram.signaling import (
    build_decline_request,
    classify_call_update,
    decline_call,
)


class FakeTelethonClient:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def __call__(self, request: object) -> None:
        self.requests.append(request)


def test_classify_incoming_ringing_call() -> None:
    event = classify_call_update(fake_call_update("PhoneCallRequested"))
    assert event is not None
    assert event.state == "ringing"
    assert event.direction == "incoming"
    assert event.call_id == 777
    assert event.peer_id == 222


def test_classify_discarded_call() -> None:
    event = classify_call_update(fake_call_update("PhoneCallDiscarded"))
    assert event is not None
    assert event.state == "discarded"


def test_ignore_non_call_updates() -> None:
    class UpdateNewMessage:
        pass

    assert classify_call_update(UpdateNewMessage()) is None


def test_decline_request_targets_the_peer() -> None:
    request = build_decline_request(777, 333)
    assert type(request).__name__ == "DiscardCallRequest"
    assert type(request.peer).__name__ == "InputPhoneCall"
    assert request.peer.id == 777
    assert request.peer.access_hash == 333
    assert request.duration == 0


def test_decline_call_invokes_mtproto() -> None:
    client = FakeTelethonClient()
    event = classify_call_update(fake_call_update())
    assert event is not None

    assert asyncio.run(decline_call(client, event)) is True
    assert len(client.requests) == 1


def test_call_manager_declines_incoming_call(settings: Settings) -> None:
    asyncio.run(_call_manager_scenario(settings))


async def _call_manager_scenario(settings: Settings) -> None:
    init_database(settings)
    client = FakeTelethonClient()
    telegram = FakeTelegram(ready=True)
    telegram.client = client  # type: ignore[assignment]

    manager = CallManager(settings, telegram)  # type: ignore[arg-type]
    await manager.handle_update(fake_call_update("PhoneCallRequested"))

    assert client.requests
    assert telegram.sent  # the owner is told why
    calls, total = manager.history()
    assert total == 1
    assert calls[0].state == "declined"
    assert "live" in (calls[0].reason or "").lower()


def test_call_manager_refuses_to_dial_without_transport(settings: Settings) -> None:
    manager = CallManager(settings, FakeTelegram(ready=True))  # type: ignore[arg-type]
    started, detail = asyncio.run(manager.initiate("reminder"))
    assert started is False
    assert "cannot" in detail.lower() or "not verified" in detail.lower()


def test_transport_diagnostics_report_native_library(settings: Settings) -> None:
    manager = CallManager(settings, FakeTelegram())  # type: ignore[arg-type]
    diagnostics = manager.transport_diagnostics()
    assert diagnostics["available"] is True
    assert diagnostics["version"]
    assert diagnostics["media_enabled"] is False


def test_account_reports_disabled_state(settings: Settings) -> None:
    account = TelegramAccount(settings)
    detail = asyncio.run(account.start())
    assert "disabled" in detail.lower()
    assert account.ready is False
    assert asyncio.run(account.send_message("hello")) is False
    status = account.status()
    assert status["enabled"] is False
    assert status["connected"] is False


# --------------------------------------------------------------------------- #
# Connection lifecycle: Telethon's synchronous helpers must not be awaited
# --------------------------------------------------------------------------- #
def _telegram_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Credential-shaped settings that never leave the test process."""
    values: dict[str, Any] = {
        "_env_file": None,
        "telegram_enabled": True,
        "telegram_api_id": 12345,
        "telegram_api_hash": "unit-test-api-hash",
        "telegram_phone": "+998900000000",
        "telegram_session_path": str(tmp_path / "ksenus"),
    }
    values.update(overrides)
    return Settings(**values)


class SyncTelethonClient:
    """Telethon-shaped client whose ``is_connected`` is synchronous.

    This mirrors Telethon 1.45, where awaiting ``is_connected()`` raises
    ``TypeError: 'bool' object can't be awaited`` and ``disconnect()`` is a
    plain method call.
    """

    def __init__(
        self,
        *,
        authorized: bool = True,
        connect_error: Exception | None = None,
        async_is_connected: bool = False,
    ) -> None:
        self.authorized = authorized
        self.connect_error = connect_error
        self.async_is_connected = async_is_connected
        self.connected = False
        self.disconnected = False
        self.sent: list[tuple[int, str]] = []
        self.handlers: list[object] = []
        self.me = SimpleNamespace(id=999, first_name="Owner")

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def is_connected(self):
        if self.async_is_connected:

            async def _check() -> bool:
                return self.connected

            return _check()
        return self.connected

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):
        return self.me

    async def send_message(self, target: int, text: str) -> None:
        self.sent.append((target, text))

    def add_event_handler(self, handler: object, event: object) -> None:
        self.handlers.append(handler)

    def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False


def test_account_connects_with_synchronous_is_connected(tmp_path: Path) -> None:
    settings = _telegram_settings(tmp_path)
    client = SyncTelethonClient()
    account = TelegramAccount(settings, client=client)

    detail = asyncio.run(account.start())

    assert detail == "connected"
    assert account.ready is True
    assert account.owner_id == 999
    assert account.self_id == 999
    # messages, two call updates, and the update-download button callback
    assert len(client.handlers) == 4
    assert any(
        getattr(handler, "__name__", "") == "_handle_update_callback"
        for handler in client.handlers
    )
    status = account.status()
    assert status["connected"] is True
    assert status["authorized"] is True

    asyncio.run(account.stop())
    assert client.disconnected is True
    assert account.ready is False


def test_account_accepts_awaitable_is_connected_as_well(tmp_path: Path) -> None:
    settings = _telegram_settings(tmp_path)
    account = TelegramAccount(settings, client=SyncTelethonClient(async_is_connected=True))

    assert asyncio.run(account.start()) == "connected"
    assert account.ready is True


def test_account_reports_unauthorized_session_without_raising(tmp_path: Path) -> None:
    settings = _telegram_settings(tmp_path)
    account = TelegramAccount(settings, client=SyncTelethonClient(authorized=False))

    detail = asyncio.run(account.start())

    assert "not authorized" in detail.lower()
    assert account.ready is False
    assert account.status()["connected"] is True
    assert account.status()["authorized"] is False


def test_status_reports_session_file_presence(tmp_path: Path) -> None:
    settings = _telegram_settings(tmp_path)
    account = TelegramAccount(settings)
    session_file = settings.telegram_session_file.with_suffix(".session")

    assert account.status()["session_present"] is False
    session_file.write_bytes(b"")
    assert account.status()["session_present"] is True


def test_connection_failure_logs_the_full_traceback(tmp_path: Path, caplog) -> None:
    settings = _telegram_settings(tmp_path)
    client = SyncTelethonClient(
        connect_error=TypeError("'bool' object can't be awaited")
    )
    account = TelegramAccount(settings, client=client)

    with caplog.at_level(logging.ERROR, logger="telegram.account"):
        detail = asyncio.run(account.start())

    assert detail == "connection failed: TypeError"
    assert account.ready is False
    failures = [
        record
        for record in caplog.records
        if record.name == "telegram.account" and "connection failed" in record.getMessage()
    ]
    assert failures, caplog.text
    assert failures[0].exc_info is not None
    rendered = "".join(traceback.format_exception(*failures[0].exc_info))
    assert "Traceback" in rendered
    assert "connect_error" in rendered


def test_credentials_never_reach_logs_or_status(tmp_path: Path, caplog) -> None:
    api_hash = "unit-test-api-hash-value"
    phone = "+998900000000"
    settings = _telegram_settings(
        tmp_path, telegram_api_hash=api_hash, telegram_phone=phone
    )
    client = SyncTelethonClient(connect_error=RuntimeError("boom"))
    account = TelegramAccount(settings, client=client)

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(account.start()) == "connection failed: RuntimeError"

    logged = caplog.text
    assert api_hash not in logged
    assert phone not in logged
    assert api_hash not in str(account.status())
    assert phone not in str(account.status())


def test_server_starts_and_flags_missing_telegram_connection(tmp_path: Path) -> None:
    settings = _telegram_settings(tmp_path)
    client = SyncTelethonClient(
        connect_error=TypeError("'bool' object can't be awaited")
    )
    runtime = AgentRuntime(
        settings,
        telegram=TelegramAccount(settings, client=client),
        speech=FakeSpeech(),
    )
    app = create_app(settings, runtime=runtime)

    with TestClient(app) as http:
        health = http.get("/health")
        status = http.get("/telegram/status")
        control = http.get("/control/status")

    assert health.status_code == 200
    body = status.json()
    assert body["connected"] is False
    assert body["authorized"] is False
    assert body["session_present"] is False
    assert "TypeError" in body["detail"]
    assert control.json()["telegram_connected"] is False
