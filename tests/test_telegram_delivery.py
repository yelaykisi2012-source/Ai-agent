"""Telegram delivery tests: owner-only, formats, failures, duplicates."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from config import Settings
from telegram.account import TelegramAccount
from telegram.delivery import TelegramDelivery
from tools import _send_to_telegram


class _FakeAccount:
    """Records what would be sent; can simulate a Telegram rejection."""

    def __init__(self, *, ready: bool = True, fail: bool = False) -> None:
        self.ready = ready
        self.fail = fail
        self.messages: list[tuple[int | None, str]] = []
        self.documents: list[tuple[int | None, str, bytes, str | None]] = []

    async def send_message(self, text: str, user_id: int | None = None) -> bool:
        self.messages.append((user_id, text))
        return not self.fail

    async def send_document(
        self,
        filename: str,
        data: bytes,
        *,
        caption: str | None = None,
        user_id: int | None = None,
    ) -> bool:
        self.documents.append((user_id, filename, data, caption))
        return not self.fail


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "telegram_owner_user_id": 4_242_424,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _delivery(account: _FakeAccount, **overrides: object) -> TelegramDelivery:
    return TelegramDelivery(_settings(**overrides), account)


def test_short_text_is_sent_as_a_message_to_the_owner() -> None:
    account = _FakeAccount()
    result = asyncio.run(_delivery(account).send(kind="text", content="Ertaga soat 9 da uchrashamiz."))

    assert result.ok is True
    assert "private Telegram chat" in result.detail
    assert account.messages == [(4_242_424, "Ertaga soat 9 da uchrashamiz.")]
    assert account.documents == []


def test_long_text_becomes_a_txt_file() -> None:
    account = _FakeAccount()
    long_text = "Qator matni. " * 600  # > 4096 characters

    result = asyncio.run(_delivery(account).send(kind="text", content=long_text, filename="reja"))

    assert result.ok is True
    assert account.messages == []
    _user, name, data, _caption = account.documents[0]
    assert name.endswith(".txt")
    assert name.startswith("reja-")
    assert data.decode("utf-8").strip() == long_text.strip()


def test_code_is_sent_as_a_python_file() -> None:
    account = _FakeAccount()
    code = "def salom():\n    return 'salom'\n"

    result = asyncio.run(_delivery(account).send(kind="code", content=code, filename="yordamchi"))

    assert result.ok is True
    _user, name, data, _caption = account.documents[0]
    assert name.endswith(".py")
    assert data.decode("utf-8") == code


def test_table_is_sent_as_xlsx_with_cells() -> None:
    from openpyxl import load_workbook

    account = _FakeAccount()
    rows = [["Sana", "Ish"], ["2026-11-16", "Qo'ng'iroq"], ["2026-11-17", "Hisobot"]]

    result = asyncio.run(_delivery(account).send(kind="table", rows=rows))

    assert result.ok is True
    _user, name, data, _caption = account.documents[0]
    assert name.endswith(".xlsx")
    sheet = load_workbook(io.BytesIO(data)).active
    assert [cell.value for cell in sheet[1]] == ["Sana", "Ish"]
    assert [cell.value for cell in sheet[3]] == ["2026-11-17", "Hisobot"]


def test_table_is_sent_as_csv_when_requested() -> None:
    account = _FakeAccount()
    rows = [["Sana", "Ish"], ["2026-11-16", "Qo'ng'iroq"]]

    result = asyncio.run(
        _delivery(account).send(kind="table", rows=rows, table_format="csv", filename="jadval")
    )

    assert result.ok is True
    _user, name, data, _caption = account.documents[0]
    assert name.endswith(".csv")
    text = data.decode("utf-8")
    assert "Sana,Ish" in text
    assert "2026-11-16,Qo'ng'iroq" in text


def test_large_file_is_sent_intact() -> None:
    account = _FakeAccount()
    payload = "x" * 400_000

    result = asyncio.run(_delivery(account).send(kind="file", content=payload, filename="katta.txt"))

    assert result.ok is True
    assert result.size == 400_000
    assert account.documents[0][2] == payload.encode("utf-8")


def test_file_over_the_limit_is_refused() -> None:
    account = _FakeAccount()
    delivery = _delivery(account, telegram_delivery_max_mb=1)

    result = asyncio.run(
        delivery.send(kind="file", content="y" * (1024 * 1024 + 10), filename="juda-katta.txt")
    )

    assert result.ok is False
    assert "limit" in result.detail
    assert account.documents == []


def test_telegram_rejection_is_reported_honestly() -> None:
    account = _FakeAccount(fail=True)

    result = asyncio.run(_delivery(account).send(kind="code", content="print(1)"))

    assert result.ok is False
    assert result.detail.startswith("Not sent:")
    assert "Sent " not in result.detail


def test_missing_owner_id_sends_nothing() -> None:
    account = _FakeAccount()

    result = asyncio.run(
        _delivery(account, telegram_owner_user_id=None).send(kind="text", content="Salom")
    )

    assert result.ok is False
    assert "TELEGRAM_OWNER_USER_ID" in result.detail
    assert account.messages == [] and account.documents == []


def test_disconnected_account_sends_nothing() -> None:
    account = _FakeAccount(ready=False)

    result = asyncio.run(_delivery(account).send(kind="text", content="Salom"))

    assert result.ok is False
    assert "not connected" in result.detail
    assert account.messages == []


def test_the_same_item_is_not_sent_twice() -> None:
    account = _FakeAccount()
    delivery = _delivery(account)

    first = asyncio.run(delivery.send(kind="code", content="print('bir marta')"))
    second = asyncio.run(delivery.send(kind="code", content="print('bir marta')"))

    assert first.ok is True
    assert second.ok is True
    assert "did not send a duplicate" in second.detail
    assert len(account.documents) == 1


def test_secret_like_content_is_refused() -> None:
    account = _FakeAccount()
    delivery = _delivery(account)

    leaked = asyncio.run(
        delivery.send(kind="text", content="OPENROUTER_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123")
    )
    as_file = asyncio.run(
        delivery.send(kind="file", content="password: supersecret123", filename="config.txt")
    )

    assert leaked.ok is False and as_file.ok is False
    assert "looks like an API key" in leaked.detail
    assert account.messages == [] and account.documents == []


def test_filename_cannot_escape_the_sandbox() -> None:
    account = _FakeAccount()

    result = asyncio.run(
        _delivery(account).send(kind="file", content="matn", filename="../../etc/passwd.txt")
    )

    assert result.ok is True
    name = account.documents[0][1]
    assert "/" not in name and ".." not in name
    assert name.endswith(".txt")


def test_unsupported_extension_is_refused() -> None:
    account = _FakeAccount()

    result = asyncio.run(_delivery(account).send(kind="file", content="MZ", filename="virus.exe"))

    assert result.ok is False
    assert ".exe" in result.detail
    assert account.documents == []


# --------------------------------------------------------------------------- #
# Tool wrapper (what the model calls)
# --------------------------------------------------------------------------- #
def test_tool_returns_an_honest_success_detail() -> None:
    account = _FakeAccount()
    context = SimpleNamespace(delivery=_delivery(account))

    detail = asyncio.run(
        _send_to_telegram(context, {"kind": "code", "content": "print('salom')"})
    )

    assert detail.startswith("Sent kod-")
    assert ".py" in detail


def test_tool_reports_a_failure_without_claiming_success() -> None:
    account = _FakeAccount(fail=True)
    context = SimpleNamespace(delivery=_delivery(account))

    detail = asyncio.run(_send_to_telegram(context, {"kind": "text", "content": "Salom"}))

    assert detail.startswith("Not sent:")


def test_tool_without_delivery_is_unavailable() -> None:
    detail = asyncio.run(
        _send_to_telegram(SimpleNamespace(delivery=None), {"kind": "text", "content": "Salom"})
    )

    assert "not available" in detail


# --------------------------------------------------------------------------- #
# Account layer: success requires a real message object
# --------------------------------------------------------------------------- #
def test_account_requires_a_message_object_for_success() -> None:
    class _Client:
        def __init__(self, message: object) -> None:
            self._message = message

        async def send_file(self, *args, **kwargs):
            return self._message

    account = TelegramAccount(_settings(), client=_Client(None))
    account._connected = True
    account._authorized = True

    assert asyncio.run(account.send_document("a.txt", b"x")) is False

    account._client = _Client(SimpleNamespace(id=1))
    assert asyncio.run(account.send_document("a.txt", b"x")) is True


# --------------------------------------------------------------------------- #
# Prompt rules: the model must be told how to send, and guests never are
# --------------------------------------------------------------------------- #
def test_owner_prompt_requires_the_delivery_tool() -> None:
    from system_prompt import build_system_prompt

    prompt = build_system_prompt(_settings(), voice=True)

    assert "send_to_my_telegram" in prompt
    assert 'only when the tool result starts with "Sent "' in prompt


def test_guest_prompt_does_not_mention_the_delivery_tool() -> None:
    from system_prompt import build_system_prompt

    prompt = build_system_prompt(_settings(), guest=True)

    assert "send_to_my_telegram" not in prompt


def test_prompt_names_ksenus_and_her_creator() -> None:
    from system_prompt import build_system_prompt

    prompt = build_system_prompt(_settings(), voice=True)

    assert "Your name is Ksenus" in prompt
    assert "Asadbek created you" in prompt


class _FlakyAccount(_FakeAccount):
    """Fails the first upload, the way a dropped connection does."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send_document(self, filename, data, *, caption=None, user_id=None) -> bool:
        self.attempts += 1
        if self.attempts == 1:
            return False
        return await super().send_document(filename, data, caption=caption, user_id=user_id)


def test_a_dropped_upload_is_retried_once(monkeypatch) -> None:
    import telegram.delivery as delivery_module

    monkeypatch.setattr(delivery_module, "_RETRY_PAUSE_SECONDS", 0.0)
    account = _FlakyAccount()

    result = asyncio.run(_delivery(account).send(kind="code", content="print(1)"))

    assert result.ok is True
    assert account.attempts == 2
    assert len(account.documents) == 1


def test_a_delivery_is_written_to_the_execution_history() -> None:
    events: list[dict[str, str]] = []
    account = _FakeAccount()
    delivery = TelegramDelivery(
        _settings(), account, recorder=lambda **event: events.append(event)
    )

    asyncio.run(delivery.send(kind="text", content="Eslatma: hisobot"))

    assert events and events[0]["message"] == "Sent a message"


def test_call_turns_use_the_configured_call_model() -> None:
    from brain import AgentBrain
    from fakes import FakeOpenAI, text_completion

    client = FakeOpenAI([text_completion("ok")])
    brain = AgentBrain(_settings(call_llm_model="fast/call-model"), client=client)

    asyncio.run(brain.reply(history=[], user_message="Salom", voice_mode=True))

    assert client.requests[0]["model"] == "fast/call-model"


def test_chat_turns_ignore_the_call_model() -> None:
    from brain import AgentBrain
    from fakes import FakeOpenAI, text_completion

    client = FakeOpenAI([text_completion("ok")])
    settings = _settings(call_llm_model="fast/call-model")
    brain = AgentBrain(settings, client=client)

    asyncio.run(brain.reply(history=[], user_message="Salom"))

    assert client.requests[0]["model"] == settings.chat_model
