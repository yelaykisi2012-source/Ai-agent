"""Talking to other people — mock tests.

Per-person sessions and history, guest rules (no tools, no owner data), and the
call-consent flow. Everything runs offline: fake Telegram, fake OpenAI, a
temporary database. A real Telegram account is never touched here; what still
needs one is listed in ``scripts/live_call_check.py`` and in the report.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import memory
from brain import AgentBrain
from config import Settings
from database import init_database
from fakes import FakeOpenAI, FakeSpeech, FakeTelegram, text_completion
from privacy import REDACTED, redact
from runtime import AgentRuntime
from telegram.account import TelegramAccount
from telegram.calls import CallManager
from test_voice_call import FakeAccount, FakeCallTransport

OWNER = 111
GUEST = 222
STRANGER = 333
OWNER_PHONE = "+998901234567"


def _settings(settings: Settings, *, public: bool = True) -> Settings:
    settings.telegram_owner_user_id = OWNER
    settings.telegram_allowed_user_ids = [GUEST]
    settings.telegram_public_chat_enabled = public
    settings.telegram_phone = OWNER_PHONE
    return settings


def _runtime(
    settings: Settings, responses: list[Any]
) -> tuple[AgentRuntime, FakeOpenAI]:
    client = FakeOpenAI(responses)
    brain = AgentBrain(settings, client=client)
    runtime = AgentRuntime(
        settings, brain=brain, telegram=FakeTelegram(), speech=FakeSpeech()
    )
    return runtime, client


def _all_text(request: dict[str, Any]) -> str:
    return " ".join(str(message.get("content", "")) for message in request["messages"])


def _recording_manager(runtime: AgentRuntime) -> list[tuple[str, int | None]]:
    """Replace the real dial-out with a recorder; nothing is ever dialled."""
    calls: list[tuple[str, int | None]] = []

    async def fake_place(reason: str = "owner request", *, peer_id: int | None = None):
        calls.append((reason, peer_id))
        return True, "calling"

    runtime.calls.place = fake_place  # type: ignore[method-assign]
    return calls


# --------------------------------------------------------------------------- #
# 1. Who is allowed to talk at all
# --------------------------------------------------------------------------- #
def test_sender_role_separates_owner_allowed_guest_and_stranger(
    settings: Settings,
) -> None:
    settings = _settings(settings, public=False)
    account = TelegramAccount(settings, client=object())

    assert account.sender_role(OWNER) == "owner"
    assert account.sender_role(GUEST) == "guest"  # on TELEGRAM_ALLOWED_USER_IDS
    assert account.sender_role(STRANGER) is None  # not allowed, no public chat
    assert account.sender_role(None) is None
    assert account.sender_role("not-a-number") is None


def test_a_second_owner_account_counts_as_the_owner(settings: Settings) -> None:
    # One person, two accounts: TELEGRAM_OWNER_USER_IDS adds the second one.
    second = 444
    settings = _settings(settings, public=False)
    settings.telegram_owner_user_ids = [second]
    account = TelegramAccount(settings, client=object())

    assert account.sender_role(OWNER) == "owner"
    assert account.sender_role(second) == "owner"
    assert account.sender_role(STRANGER) is None


def test_public_chat_lets_any_person_in_as_a_guest(settings: Settings) -> None:
    account = TelegramAccount(_settings(settings, public=True), client=object())

    assert account.sender_role(STRANGER) == "guest"
    assert account.sender_role(OWNER) == "owner"


def test_a_stranger_is_ignored_when_public_chat_is_off(settings: Settings) -> None:
    asyncio.run(_stranger_ignored(_settings(settings, public=False)))


async def _stranger_ignored(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(settings, [text_completion("Bu javob chiqmasligi kerak")])

    reply = await runtime.handle_telegram_message("Salom", STRANGER)

    assert reply is None
    assert client.requests == [], "the brain was asked to answer a stranger"
    assert runtime._chat_conversations == {}


def test_an_allowed_guest_is_answered(settings: Settings) -> None:
    asyncio.run(_guest_answered(_settings(settings, public=False)))


async def _guest_answered(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(settings, [text_completion("Assalomu alaykum!")])

    reply = await runtime.handle_telegram_message("Salom", GUEST)

    assert reply == "Assalomu alaykum!"
    assert len(client.requests) == 1
    assert runtime._chat_conversations == {GUEST: runtime._chat_conversations[GUEST]}


# --------------------------------------------------------------------------- #
# 2. One session, one history, one call state per person
# --------------------------------------------------------------------------- #
def test_two_people_never_share_a_conversation(settings: Settings) -> None:
    asyncio.run(_separate_conversations(_settings(settings)))


async def _separate_conversations(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(
        settings,
        [
            text_completion("Salom, egam."),  # owner, turn 1
            text_completion("Salom, mehmon."),  # guest, turn 1
            text_completion("Xotiramda faqat sizning gaplaringiz."),  # guest, turn 2
        ],
    )

    await runtime.handle_telegram_message("Egaga maxfiy vazifa: parolni eslab qol", OWNER)
    await runtime.handle_telegram_message("Salom, men boshqa odamman", GUEST)
    await runtime.handle_telegram_message("Men kimman?", GUEST)

    owner_conversation = runtime._chat_conversations[OWNER]
    guest_conversation = runtime._chat_conversations[GUEST]
    assert owner_conversation != guest_conversation

    # The guest's own conversation holds only the guest's messages.
    stored = memory.recent_messages(guest_conversation, 20)
    texts = [item.content for item in stored]
    assert "Salom, men boshqa odamman" in texts
    assert "Men kimman?" in texts
    assert not any("parolni" in text for text in texts)

    # The third request carries the guest's history and none of the owner's.
    third = _all_text(client.requests[2])
    assert "Salom, men boshqa odamman" in third
    assert "parolni" not in third
    assert "egam" not in third

    # The guest kept one conversation across turns; the owner kept another.
    assert len(runtime._chat_conversations) == 2


def test_two_calls_keep_separate_call_state(settings: Settings) -> None:
    asyncio.run(_separate_call_state(_settings(settings)))


async def _separate_call_state(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(
        settings,
        [
            text_completion("Ha, egam, eshitaman."),  # owner's call
            text_completion("Ha, mehmon, eshitaman."),  # guest's call
        ],
    )

    owner_reply = await runtime.handle_call_message("Salom", OWNER)
    guest_reply = await runtime.handle_call_message("Salom", GUEST)

    assert owner_reply == "Ha, egam, eshitaman."
    assert guest_reply == "Ha, mehmon, eshitaman."
    assert runtime._call_conversations[OWNER] != runtime._call_conversations[GUEST]
    assert runtime._call_conversations[OWNER] != runtime._chat_conversations.get(OWNER)

    # The guest's call turn has no tools and the guest rules.
    guest_request = client.requests[1]
    assert guest_request.get("tools") is None
    assert "NOT the owner" in guest_request["messages"][0]["content"]
    # The owner's call turn keeps the tools.
    assert client.requests[0].get("tools")


def test_a_call_with_no_reported_peer_stays_the_owner_path(settings: Settings) -> None:
    """The older one-argument call path must not suddenly become a guest."""
    asyncio.run(_legacy_call_path(_settings(settings)))


async def _legacy_call_path(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(settings, [text_completion("Ha, eshitaman.")])

    reply = await runtime.handle_call_message("Salom")

    assert reply == "Ha, eshitaman."
    assert client.requests[0].get("tools"), "the owner lost their tools"
    assert runtime._call_conversations == {0: runtime._call_conversations[0]}


# --------------------------------------------------------------------------- #
# 3. A guest cannot reach the owner's data
# --------------------------------------------------------------------------- #
def test_a_guest_gets_no_tools_and_the_guest_rules(settings: Settings) -> None:
    asyncio.run(_guest_rules(_settings(settings)))


async def _guest_rules(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(settings, [text_completion("Salom!")])

    await runtime.handle_telegram_message("Vazifa qo‘shib ber", GUEST)

    request = client.requests[0]
    assert request.get("tools") is None
    system = request["messages"][0]["content"]
    assert "NOT the owner" in system
    assert "Never reveal the owner's personal data" in system
    assert "You have no tools in this conversation" in system


def test_secrets_and_the_owner_phone_are_redacted_for_a_guest(
    settings: Settings,
) -> None:
    asyncio.run(_guest_redaction(_settings(settings)))


async def _guest_redaction(settings: Settings) -> None:
    init_database(settings)
    leaked = (
        "Mening kalitim sk-abcdefghijklmnop1234567890, "
        "fayl F:\\Vs code\\Ai agent\\data\\agent.db da, "
        f"telefonim {OWNER_PHONE}."
    )
    runtime, _client = _runtime(settings, [text_completion(leaked)])

    reply = await runtime.handle_telegram_message("Kalitingni ber", GUEST)

    assert reply is not None
    assert "sk-abcdefghijklmnop" not in reply
    assert OWNER_PHONE not in reply
    assert "agent.db" not in reply
    assert REDACTED in reply


def test_the_owner_still_sees_their_own_answer_unchanged(settings: Settings) -> None:
    asyncio.run(_owner_not_redacted(_settings(settings)))


async def _owner_not_redacted(settings: Settings) -> None:
    init_database(settings)
    text = f"Telefon raqamingiz {OWNER_PHONE}."
    runtime, _client = _runtime(settings, [text_completion(text)])

    reply = await runtime.handle_telegram_message("Telefonim qaysi?", OWNER)

    assert reply == text


def test_a_guest_cannot_confirm_an_owner_action(settings: Settings) -> None:
    asyncio.run(_guest_cannot_confirm(_settings(settings)))


async def _guest_cannot_confirm(settings: Settings) -> None:
    init_database(settings)
    action_id = memory.create_pending_action(
        "send_telegram_message", {"text": "maxfiy"}, "Send a secret message"
    )
    runtime, _client = _runtime(settings, [text_completion("Tushunarli.")])

    reply = await runtime.handle_telegram_message("confirm", GUEST)

    assert reply == "Tushunarli."  # it went to the chat, not to the action
    assert memory.get_pending_action(action_id) is not None, "a guest confirmed it"


@pytest.mark.parametrize(
    "text,expect_secret",
    [
        ("Oddiy javob, hech narsa yo‘q.", False),
        ("Kalit: sk-abcdefghijklmnop1234567890", True),
        ("Authorization: Bearer abcdef0123456789abcdef", True),
        ("OPENAI_API_KEY=sk-abcdefghijklmnop1234567890", True),
        ("Fayl: F:\\Vs code\\Ai agent\\data\\agent.db", True),
        ("Fayl: /home/user/agent/data/agent.db", True),
    ],
)
def test_redaction_finds_the_shapes_that_must_not_leave(text: str, expect_secret: bool) -> None:
    assert redact("x")[1] is False
    cleaned, changed = redact(text)
    assert changed is expect_secret
    if expect_secret:
        assert REDACTED in cleaned


# --------------------------------------------------------------------------- #
# 4. Calling a person only after they agreed
# --------------------------------------------------------------------------- #
def test_a_guest_call_needs_an_offer_and_an_explicit_yes(settings: Settings) -> None:
    asyncio.run(_consent_flow(_settings(settings, public=False)))


async def _consent_flow(settings: Settings) -> None:
    init_database(settings)
    runtime, client = _runtime(settings, [text_completion("Bu javob kerak emas.")])
    dialled = _recording_manager(runtime)

    # "ha" with no offer on the table is just a chat message.
    reply = await runtime.handle_telegram_message("ha", GUEST)
    assert reply == "Bu javob kerak emas."
    assert dialled == []
    assert runtime.calls.has_call_consent(GUEST) is False

    # Asking to be called produces a question, not a call.
    offer = await runtime.handle_telegram_message("Menga qo‘ng‘iroq qiling", GUEST)
    assert offer is not None and "qo‘ng‘iroq" in offer
    assert dialled == [], "a call was placed before consent"
    assert runtime.calls.has_call_consent(GUEST) is False
    assert len(client.requests) == 1, "the offer itself was sent to the brain"

    # An explicit yes grants consent and dials that person only.
    agreed = await runtime.handle_telegram_message("ha", GUEST)
    assert agreed == "Xo‘p, qo‘ng‘iroq qilayapman."
    assert dialled == [("the guest agreed to a call", GUEST)]
    assert runtime.calls.has_call_consent(GUEST) is True
    assert runtime._call_offers == {}


def test_a_guest_who_refuses_is_not_called(settings: Settings) -> None:
    asyncio.run(_refused_flow(_settings(settings, public=False)))


async def _refused_flow(settings: Settings) -> None:
    init_database(settings)
    runtime, _client = _runtime(settings, [])
    dialled = _recording_manager(runtime)

    await runtime.handle_telegram_message("Menga qo‘ng‘iroq qiling", GUEST)
    reply = await runtime.handle_telegram_message("yo‘q", GUEST)

    assert reply == "Xo‘p, qo‘ng‘iroq qilmayman."
    assert dialled == []
    assert runtime.calls.has_call_consent(GUEST) is False
    assert runtime._call_offers == {}


def test_an_offer_expires_without_an_answer(settings: Settings) -> None:
    asyncio.run(_expired_offer(_settings(settings, public=False)))


async def _expired_offer(settings: Settings) -> None:
    init_database(settings)
    settings.telegram_call_consent_seconds = 600
    runtime, client = _runtime(settings, [text_completion("Xo‘p, gaplashamiz.")])
    dialled = _recording_manager(runtime)

    await runtime.handle_telegram_message("call me", GUEST)
    # The offer is older than the window, so "ha" is an ordinary word again.
    runtime._call_offers[GUEST] = time.monotonic() - settings.telegram_call_consent_seconds - 1

    reply = await runtime.handle_telegram_message("ha", GUEST)

    assert reply == "Xo‘p, gaplashamiz."
    assert dialled == []
    assert runtime.calls.has_call_consent(GUEST) is False


def test_a_failed_guest_call_does_not_leave_consent_behind(settings: Settings) -> None:
    asyncio.run(_failed_call(_settings(settings, public=False)))


async def _failed_call(settings: Settings) -> None:
    init_database(settings)
    runtime, _client = _runtime(settings, [])

    async def failing_place(reason: str = "x", *, peer_id: int | None = None):
        return False, "TELEGRAM_CALLS_ENABLED=false. Nothing was dialled."

    runtime.calls.place = failing_place  # type: ignore[method-assign]

    await runtime.handle_telegram_message("Menga qo‘ng‘iroq qiling", GUEST)
    reply = await runtime.handle_telegram_message("ha", GUEST)

    # The technical reason names a setting; a stranger must not see it.
    assert reply == "Uzr, hozir qo‘ng‘iroq qilib bo‘lmadi."
    assert "TELEGRAM_CALLS_ENABLED" not in (reply or "")
    assert runtime.calls.has_call_consent(GUEST) is False


def test_the_call_manager_refuses_a_stranger_and_needs_consent(
    settings: Settings,
) -> None:
    asyncio.run(_manager_refusals(_settings(settings, public=False)))


async def _manager_refusals(settings: Settings) -> None:
    init_database(settings)
    settings.telegram_calls_enabled = True
    settings.telegram_owner_user_id = 555
    account = FakeAccount(owner_id=555, self_id=999)
    manager = CallManager(settings, account, transport=FakeCallTransport())
    manager.bind_pipeline(FakeSpeech(), lambda text: asyncio.sleep(0, result="ok"))

    # Not on the allow list, even with a yes.
    manager.grant_call_consent(STRANGER)
    refused, detail = await manager.place("guest", peer_id=STRANGER)
    assert refused is False
    assert "not on TELEGRAM_ALLOWED_USER_IDS" in detail
    assert manager.active is False

    # On the allow list, but no yes yet.
    unconsented, reason = await manager.place("guest", peer_id=GUEST)
    assert unconsented is False
    assert "have not agreed to a call" in reason

    # With a yes, the call is allowed and the consent is used up.
    manager.grant_call_consent(GUEST)
    assert manager.has_call_consent(GUEST) is True
    started, _detail = await manager.place("guest agreed", peer_id=GUEST)
    assert started is True
    assert manager.has_call_consent(GUEST) is False, "one yes must buy one call"
    await manager.hangup()


def test_consent_expires_on_its_own(settings: Settings) -> None:
    settings = _settings(settings, public=False)
    settings.telegram_call_consent_seconds = 30
    manager = CallManager(settings, FakeTelegram(), transport=FakeCallTransport())

    manager.grant_call_consent(GUEST)
    assert manager.has_call_consent(GUEST) is True
    # Backdate the record instead of waiting 30 s.
    manager._call_consent[GUEST] = time.monotonic() - 1
    assert manager.has_call_consent(GUEST) is False
    assert manager._call_consent == {}, "an expired consent stayed in memory"


# --------------------------------------------------------------------------- #
# 5. The Telegram entry point decides who may talk
# --------------------------------------------------------------------------- #
class FakeEvent:
    """A Telethon-shaped NewMessage event."""

    def __init__(
        self,
        text: str,
        sender_id: int,
        *,
        is_group: bool = False,
        is_channel: bool = False,
        bot: bool = False,
    ) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self.is_group = is_group
        self.is_channel = is_channel
        self.sender = type("Sender", (), {"bot": bot})()
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


def test_the_account_layer_passes_the_sender_id_and_replies(settings: Settings) -> None:
    asyncio.run(_incoming_message(_settings(settings, public=False)))


async def _incoming_message(settings: Settings) -> None:
    seen: list[tuple[str, int | None]] = []

    async def on_message(text: str, user_id: int | None = None) -> str | None:
        seen.append((text, user_id))
        return "Javob"

    account = TelegramAccount(settings, client=object(), on_message=on_message)
    event = FakeEvent("Salom", GUEST)

    await account._handle_incoming(event)

    assert seen == [("Salom", GUEST)], "the sender id was not passed on"
    assert event.replies == ["Javob"]


@pytest.mark.parametrize(
    "event",
    [
        FakeEvent("Salom", STRANGER),  # not the owner, not allowed, no public chat
        FakeEvent("Salom", GUEST, is_group=True),  # group messages are ignored
        FakeEvent("Salom", GUEST, is_channel=True),  # channels too
        FakeEvent("Salom", GUEST, bot=True),  # two bots would loop forever
        FakeEvent("   ", GUEST),  # nothing to answer
    ],
    ids=["stranger", "group", "channel", "bot", "empty"],
)
def test_messages_that_must_not_reach_the_brain(settings: Settings, event: FakeEvent) -> None:
    asyncio.run(_ignored_message(_settings(settings, public=False), event))


async def _ignored_message(settings: Settings, event: FakeEvent) -> None:
    seen: list[tuple[str, int | None]] = []

    async def on_message(text: str, user_id: int | None = None) -> str | None:
        seen.append((text, user_id))
        return "Bu javob chiqmasligi kerak"

    account = TelegramAccount(settings, client=object(), on_message=on_message)

    await account._handle_incoming(event)

    assert seen == [], "a message that should be dropped reached the handler"
    assert event.replies == []


def test_a_handler_failure_never_kills_the_telegram_client(settings: Settings) -> None:
    asyncio.run(_failing_handler(_settings(settings, public=False)))


async def _failing_handler(settings: Settings) -> None:
    async def broken(text: str, user_id: int | None = None) -> str:
        raise RuntimeError("handler exploded")

    account = TelegramAccount(settings, client=object(), on_message=broken)
    event = FakeEvent("Salom", OWNER)

    await account._handle_incoming(event)

    assert event.replies == ["Sorry, something went wrong on my side."]
