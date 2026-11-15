# KSENUS AI

A personal AI assistant that runs on your Windows 11 machine: natural
conversation in Uzbek and English, durable memory, tasks and reminders,
sleep checks, voice in and out, and a dedicated Telegram **user account** for
messaging and call signaling.

This is a real application, not a demo. It is also honest about one thing it
does not do yet: **live two-way audio on Telegram 1:1 calls**. See
[Calling: verified limits](#calling-verified-limits).

## Status

| Area | State |
| --- | --- |
| AI brain (OpenAI chat, tools, retries, context) | Working, 84 tests |
| Conversation persistence + long-term memory | Working |
| Tasks, deadlines, reminders, retries | Working |
| Sleep check with honest outcomes | Working |
| Local dashboard + emergency stop | Working |
| Voice: STT, TTS, WebSocket turn loop | Working (turn-based, needs your key) |
| Telegram user-account login, messaging, call signaling | Implemented; needs your credentials |
| Telegram live call audio | **Not enabled** — see below |
| Automated tests | 84 passed, no network or credentials needed |

## What is verified vs. not

Verified on this machine:

- 84 automated tests (database, memory, brain retries/errors, tools,
  confirmation gating, reminders, sleep checks, Telegram signaling with fakes,
  speech engine with fakes, real FFmpeg OGG/Opus conversion, API security).
- The server boots, creates its schema, starts the scheduler, serves the
  dashboard and the API, and answers `/health`.

Not verified because it needs your accounts:

- Telegram login with your real `api_id` / `api_hash`.
- A real Telegram message round trip.
- A real Telegram call (signaling is implemented; audio transport is not).
- Real OpenAI chat and speech calls (your key, your credits).

## Architecture

```
F:\Vs code\Ai agent\
  main.py                    FastAPI app, routes, dashboard, WebSocket voice
  runtime.py                 Wires services; shared chat pipeline
  brain.py                   OpenAI client, retries, context, tool loop
  system_prompt.py           Persona + non-negotiable safety rules
  memory.py                  Conversations, long-term memory, runtime state
  models.py                  All ORM models
  schemas.py                 API contracts
  database.py                Engine, sessions, event log
  config.py                  Validated settings
  logging_setup.py           Console + rotating file logs
  tools.py                   Real tool handlers + confirmation gating
  integrations/openai_client.py
  tasks/
    manager.py               Task CRUD
    scheduler.py             APScheduler wrapper, restart recovery
    reminders.py             Delivery, retries, acknowledgement
    sleep_monitor.py         Sleep check, honest outcomes
  telegram/
    account.py               Telethon user-account lifecycle
    authentication.py        Interactive login / status / logout CLI
    signaling.py             phone.* signaling helpers
    calls.py                 Call policy, history, transport diagnostics
  voice/
    speech_engine.py         OpenAI transcription and synthesis
    audio.py                 Validation + FFmpeg conversion
    realtime.py              WebSocket voice loop
  dashboard/                 index.html, styles.css, app.js
  calling_service/README.md  Call research, API surface, licensing
  tests/                     84 tests
  data/                      SQLite database, Telegram session (git-ignored)
  logs/                      Rotating logs
```

## Requirements

- Windows 11, Python 3.11+ (verified on 3.14.5)
- FFmpeg on `PATH` (verified: 9.0.1) for voice notes and conversions
- An OpenAI API key
- A spare Telegram account for Ksenus, plus `api_id` / `api_hash`

## Setup (PowerShell)

```powershell
cd "F:\Vs code\Ai agent"
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env
```

Put your OpenAI key in `.env` as `OPENAI_API_KEY=...` or set it as a Windows
environment variable. Never commit `.env`.

## Run

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

- Dashboard: <http://127.0.0.1:8000/dashboard>
- API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

Tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Telegram account

Ksenus is a **user account**, not a bot. Bots cannot make or receive Telegram
voice calls at all.

1. Log in to <https://my.telegram.org/apps> with the spare account and create
   an application.
2. Put `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_PHONE` in `.env`
   (phone with country code).
3. Run the login CLI and complete it in the terminal. It asks for the code and
   the two-step password locally; neither is logged or sent to the AI:

```powershell
.\.venv\Scripts\python.exe -m telegram.authentication login
.\.venv\Scripts\python.exe -m telegram.authentication status
.\.venv\Scripts\python.exe -m telegram.authentication logout   # revokes the session
```

4. Set `TELEGRAM_ENABLED=true` and restart the app.

The login stores the session at `data/telegram/ksenus.session` and records
your Telegram user id as the only authorized owner. Messages from anyone else
are ignored.

## Calling: verified limits

Implemented and testable now:

- Detecting `updatePhoneCall` / `phoneCallRequested` updates.
- Declining an incoming call through `phone.discardCall` and telling you why.
- Call history and audit events.
- `GET /calls/diagnostics`, which reports the installed native transport
  (`ntgcalls 3.0.7`, Windows x86_64) and its protocol layers.

Not implemented:

- Two-way call audio. This needs the `ntgcalls` WebRTC transport wired to
  Telethon signaling (Diffie-Hellman exchange, `sendSignalingData`, a 10 ms
  PCM frame pump), and it must be verified with two accounts and real audio.
  Since that verification cannot be done without your accounts, Ksenus refuses
  to dial and declines calls instead of connecting a silent call. Details,
  verified API signatures, setup ordering and licensing are in
  `calling_service/README.md`.

## Voice

- `POST /voice/chat` — upload audio, get a transcript, a reply and MP3.
- `POST /voice/speak` — text to MP3.
- `POST /voice/note` — text to OGG/Opus suitable for a Telegram voice note.
- `WS /ws/voice` — browser voice loop: send binary audio chunks, then
  `{"type":"end"}`; receive transcript JSON, reply JSON, then MP3 bytes.

This is turn-based, not full-duplex barge-in. The UI and docs say so.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/chat` | Converse; returns `confirmation` when a sensitive tool is requested |
| POST | `/confirmations/{id}` | Execute an approved sensitive action exactly once |
| GET | `/conversations` | List conversations |
| GET/DELETE | `/conversations/{id}` | Transcript / delete |
| GET/POST | `/tasks` | List / create tasks |
| PATCH/POST/DELETE | `/tasks/{id}`, `/tasks/{id}/complete` | Update, complete, delete |
| GET/POST | `/reminders` | List / schedule reminders |
| POST/DELETE | `/reminders/{id}/acknowledge`, `/reminders/{id}` | Acknowledge / cancel |
| GET/POST | `/sleep/check` | Latest / start a sleep check |
| POST | `/sleep/acknowledge` | Confirm you are awake |
| GET/POST | `/memory` | List / save memories |
| GET | `/memory/search?q=` | Search memories |
| PATCH/DELETE | `/memory/{id}` | Update / delete a memory |
| GET | `/calls`, `/calls/diagnostics` | Call history and transport state |
| GET | `/telegram/status` | Connection state (never session data) |
| GET/POST | `/control/status`, `/control/stop`, `/control/resume` | Emergency stop |
| GET | `/dashboard`, `/dashboard/summary` | Dashboard and its data |

Example:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/chat `
  -ContentType "application/json" `
  -Body '{"message":"Ertaga soat 8 da meni uyg'"'"'ot"}'
```

## Security

- Secrets live in `.env` / environment variables; `.env` is git-ignored.
- The Telegram session file stays on disk under `data/` and is never returned
  by any endpoint or written to logs.
- Login codes and 2FA passwords are read from the terminal and never stored or
  sent to a model.
- Mutating endpoints require `X-API-Token` when `API_TOKEN` is set; without it
  they are restricted to loopback clients.
- `/chat` is rate limited (`CHAT_RATE_LIMIT_PER_MINUTE`).
- The dashboard displays no secrets.
- Sensitive tools require explicit confirmation, stored as a single-use pending
  action that expires in 10 minutes.
- Emergency stop pauses the scheduler and defers reminders; resume restores it.
- Every startup, notification attempt, call and failure is written to the
  `event_log` table and visible in the dashboard.

## Testing notes

- Tests never call OpenAI or Telegram. `tests/fakes.py` provides deterministic
  doubles.
- The database is a temporary file per test.
- FFmpeg is exercised for real (sine wave → OGG/Opus, asserts the `OggS`
  header).
- Chinese wall: tests distinguish mocked Telegram calls from real ones. No test
  claims a real call happened.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `python` opens Microsoft Store | Use `.\.venv\Scripts\python.exe` or `py -3.14` |
| `No module named ...` | Install into the venv, not global Python |
| Chat returns 503 "No OpenAI API key" | Set `OPENAI_API_KEY` and restart |
| Chat returns 502 "rate limit" | Quota/rate limit; retry or check billing |
| Telegram status "not authorized" | Run the login command |
| Telegram login asks for a code | Check the Telegram app on the phone, then re-run |
| Dashboard shows no data | Confirm the server is running and you opened `/dashboard` |
| Voice note fails | FFmpeg must be on `PATH` |
| `database is locked` | Only one app instance should use `data/agent.db` |

## Known limitations

- Live Telegram call audio is not enabled or verified (see above).
- Voice is turn-based, not full-duplex.
- Single-user by design: one owner Telegram id, one local operator.
- `gpt-4o-mini-tts` is scheduled for deprecation in January 2027; change
  `OPENAI_TTS_MODEL` when it retires.
- SQLite is fine for one user; moving to PostgreSQL is a `.env` change plus a
  migration pass.
- Reminder delivery depends on Telegram being connected; otherwise attempts are
  logged as undelivered and retried honestly.
