"""Interactive Telegram login for the dedicated Ksenus account.

Run only from a local terminal — never through the AI conversation:

    .\\.venv\\Scripts\\python.exe -m telegram.authentication login
    .\\.venv\\Scripts\\python.exe -m telegram.authentication status
    .\\.venv\\Scripts\\python.exe -m telegram.authentication logout

Secrets never touch logs: the API hash is read from ``.env``, the login code
and 2FA password are read from the terminal with ``getpass``/``input`` and are
never printed or stored. The session file stays on this computer.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from config import Settings, get_settings
from database import init_database
from logging_setup import configure_logging
from telegram.account import maybe_await

logger = logging.getLogger(__name__)


def _client(settings: Settings):
    from telethon import TelegramClient

    api_hash = settings.telegram_api_hash.get_secret_value() if settings.telegram_api_hash else ""
    return TelegramClient(
        str(settings.telegram_session_file),
        settings.telegram_api_id,
        api_hash,
        device_model="KSENUS AI",
        system_version="Windows 11",
    )


async def login(settings: Settings) -> int:
    """Perform an interactive login. Requires api_id/api_hash in .env."""
    if not settings.telegram_configured:
        print(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are not set.\n"
            "Create an application at https://my.telegram.org/apps and put both\n"
            "values in .env. Then run this command again."
        )
        return 2

    from telethon.errors import SessionPasswordNeededError

    phone = settings.telegram_phone or input("Phone number (with country code, e.g. +998...): ").strip()
    if not phone:
        print("A phone number is required.")
        return 2

    client = _client(settings)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Already authorized as {getattr(me, 'first_name', '')} (id={getattr(me, 'id', '?')}).")
            await _persist_owner(settings, getattr(me, "id", None))
            return 0

        await client.send_code_request(phone)
        print("Telegram sent a login code to your Telegram app (not by SMS usually).")
        code = input("Login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Two-step verification password (not echoed): ")
            await client.sign_in(password=password)

        me = await client.get_me()
        print(f"Logged in as {getattr(me, 'first_name', '')} (id={getattr(me, 'id', '?')}).")
        await _persist_owner(settings, getattr(me, "id", None))
        print(
            "Session saved locally. Add TELEGRAM_ENABLED=true to .env, then restart the agent.\n"
            "The owner id was stored, so only your account can command Ksenus."
        )
        return 0
    finally:
        await maybe_await(client.disconnect())


async def _persist_owner(settings: Settings, owner_id: int | None) -> None:
    """Remember which account owns Ksenus, without printing secrets."""
    if owner_id is None:
        return
    from memory import set_state

    init_database(settings)
    set_state("telegram_owner_user_id", str(owner_id))


async def status(settings: Settings) -> int:
    if not settings.telegram_configured:
        print("Telegram is not configured (missing api_id/api_hash).")
        return 2
    session_file = settings.telegram_session_file.with_suffix(".session")
    print(f"session file: {'present' if session_file.is_file() else 'absent'}")
    client = _client(settings)
    await client.connect()
    try:
        authorized = await client.is_user_authorized()
        print("configured: yes")
        print(f"authorized: {'yes' if authorized else 'no'}")
        if authorized:
            me = await client.get_me()
            print(f"account: {getattr(me, 'first_name', '')} (id={getattr(me, 'id', '?')})")
        return 0 if authorized else 1
    finally:
        await maybe_await(client.disconnect())


async def logout(settings: Settings) -> int:
    if not settings.telegram_configured:
        print("Telegram is not configured.")
        return 2
    client = _client(settings)
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
            print("Logged out and revoked the session on Telegram's side.")
        else:
            print("No authorized session was present.")
    finally:
        await maybe_await(client.disconnect())
    session_file = settings.telegram_session_file.with_suffix(".session")
    if session_file.exists():
        session_file.unlink()
        print("Local session file removed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KSENUS Telegram account authentication")
    parser.add_argument("command", choices=["login", "status", "logout"])
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)

    if args.command == "login":
        return asyncio.run(login(settings))
    if args.command == "status":
        return asyncio.run(status(settings))
    return asyncio.run(logout(settings))


if __name__ == "__main__":
    sys.exit(main())
