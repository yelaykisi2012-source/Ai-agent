"""Telegram signaling tests. No network, no credentials."""

from __future__ import annotations

import asyncio

from config import Settings
from database import init_database
from fakes import FakeTelegram, fake_call_update
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
    request = build_decline_request(222)
    assert type(request).__name__ == "DiscardCallRequest"
    assert request.peer == 222
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
