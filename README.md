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
| Uzbek TTS via VoiceLab (Aisha AI) | Working, live-tested (`TTS_PROVIDER=voicelab`) |
| Telegram user-account login, messaging, call signaling | Implemented; needs your credentials |
| Telegram live call audio | **Not enabled** — see below |
| Computer agent (shell, files, browser, system info) | Working, 476 tests |
| Telegram group chats (mention or reply only) | Working |
| Telegram group join / list (owner-confirmed) | Working |
| Web access (public search + page reading) | Working, no API key needed |
| Automated tests | 476 passed, no network or credentials needed |

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
  tests/                     476 tests
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
are ignored unless you allow them (`TELEGRAM_PUBLIC_CHAT_ENABLED` or
`TELEGRAM_ALLOWED_USER_IDS`).

A second account or phone that is still you goes in
`TELEGRAM_OWNER_USER_IDS=123,456`: those ids get the owner's full rights, so
chat and calls work from either account. Only your own ids belong there — an
ignored message is logged with the sender's numeric id, which is how a new id
is found without guessing.

### Group chats

Ksenus also answers in group chats, but only when it is addressed:

- an `@username` mention of the account, or
- a reply to a message the account sent.

It never answers every message in a group, so it cannot flood a chat. A plain
group message is logged and skipped. `TELEGRAM_GROUPS_ENABLED=false` turns
group replies off completely.

The agent can also enter a group itself, with the owner's confirmation:

- `join_group` — `@username`, `t.me/username`, `t.me/+invite` or
  `t.me/joinchat/...`. A chat id, a phone number or a bare number is refused,
  so it cannot join the wrong chat by guessing.
- `list_groups` — the groups and channels the account is already in, so it
  does not join one twice.

Example: «shu havoladan guruhga qo‘shil: https://t.me/+AbCdEf» — the tool
prepares the join, you confirm (`confirm` / `/confirm` / dashboard), and only
then does the account actually join. Anyone in the group who is not the owner is
still a guest: no tools, no owner data, redacted answers. Channels and bot
accounts are always ignored.

#### Group voice chats

The account can also join a group's voice chat (a Telegram group call), with the
owner's confirmation:

- `join_group_call` — `@username`, `t.me/username` or the group's name. The
  account must already be a member of the group; a private invite link is
  refused, so `join_group` comes first.
- `leave_group_call` — leaves the call it is in.

The join follows ntgcalls' group-call flow: `create_call` produces the join
payload, `phone.joinGroupCall` puts it on the call, and the connection
parameters Telegram returns go to `connect`. **The account joins muted and
listens; it does not speak in a group call yet.** That is a separate feature
(sending TTS audio through the capture stream) and is not implemented.

Honest limit: the join/leave flow is covered by tests against a fake Telegram
client and a fake transport, but it has **not** been run against a live group
call yet — that needs a real voice chat with the account as a member.
`TELEGRAM_GROUP_CALLS_ENABLED=false` disables the tools.

### Kompyuter rejimi (computer mode)

From Telegram: «kompyuterda ishlaydigan modelga o‘t» / «kompyuter rejimini yoq».
The agent asks for confirmation in Telegram (the normal confirm flow), and only
a confirmed action does anything:

1. The small animated agent appears on this computer's screen — and nothing
else. There is **no Telegram call** and **no panel window**: the owner asked
for the little robot itself to be the face and the voice of the mode.
2. The agent reads its answers out through this computer's own speakers
(`voice/pc_speaker.py`). The answer is synthesised by the configured TTS
backend, converted to WAV with FFmpeg and played with Windows' `winsound`;
the pet's mouth moves while the sentence is playing. Without FFmpeg or on a
non-Windows machine the agent stays silent and the mode reports
`computer_voice: false` — it never pretends to speak.
3. `stop_computer_mode`, the agent's right-click menu («Kompyuter rejimini
tugatish») or `POST /computer-mode/stop` ends the mode and stops the voice.

Safety: a second computer mode is refused, and only the owner can start one
through the confirmed tool. Commands still need the same approvals they needed
before; nothing runs on its own because the mode is on.

`COMPANION_VOICE_ENABLED=false` keeps the mode visible but silent. The
project's other call features («call me» reminders, guest calls) are separate
and unchanged; `TELEGRAM_CALLS_ENABLED=false` turns those off globally.

### Mini messenger (Windows ilova)

Not everyone who may use the agent has Telegram. `agent_messenger.py` is a small
native Windows window (Tk, no browser, no website) in the desktop client's dark
style — translucent window, rounded chat bubbles, avatars, timestamps — that
opens a chat with the agent and nothing else:

- **yozish** — a normal chat thread; every turn goes to `POST /chat` on the
  local API and keeps one `conversation_id`, so the thread stays continuous;
- **qo‘ng‘iroq** — «☎» opens a call screen with the agent's avatar and a running
  timer, pick the microphone, speak, press «To‘xtatish»: the recording goes to
  `POST /voice/transcribe`, the words go to `POST /chat`, and the answer is read
  out through the PC speakers (`POST /voice/speak` + `winsound`). After the
  answer the app starts listening again until «Tugatish»;
- a pending sensitive action shows a «Tasdiqlash» button
  (`POST /confirmations/{id}`);
- the thread is saved in `%LOCALAPPDATA%\Ksenus\history.json`, so reopening the
  window continues the same conversation; a typing bubble answers while the
  agent works, a failed turn puts the words back into the composer instead of
  losing them, and «🗑» clears the thread (second press confirms).

There is **no contact list, no recipient field, no group and no Telegram
session** in the app: it cannot message another person, by design. It talks only
to the agent on `127.0.0.1`, so the agent can show its full abilities (spoken
answers, a live call, every tool) without a phone call or a Telegram account.

**One click:** `run.bat` checks whether the local server is answering, starts
uvicorn if it is not, then opens the messenger. A Desktop and Start Menu
shortcut to `run.bat` is what «O‘rnatish» creates.

**First run:** the app opens a roomy install page that says where it installs —
the app folder, the Desktop shortcut, the Start Menu shortcut and
`%LOCALAPPDATA%\Ksenus\messenger.json`. «O‘rnatish» creates both shortcuts,
fills an animated progress bar with named steps, then draws a green check; or
«Menda Telegram bor» skips the shortcuts and goes straight to the chat. The
choice is remembered, so the page appears once; `--setup` shows it again.

**Opening:** the window fades in, and later launches show a short animated
splash (logo ring, «Ishga tushmoqda…») before the chat.

`KSENUS_WINDOW_URL`, `KSENUS_WINDOW_TOKEN` and `KSENUS_MIC` (or `--url`,
`--token`, `--mic`) override the defaults; the token is only needed when
`API_TOKEN` is set. Recording needs FFmpeg (already required for voice notes)
and a Windows microphone; device names come from
`ffmpeg -list_devices true -f dshow`. The window stores no secrets.

`KSENUS_WINDOW_URL` tells the agent which local API to talk to (default
`http://127.0.0.1:API_PORT`); set it when uvicorn runs on another port.

Live status: the agent window (transparent, sharp, mirroring, click and drag)
has been checked on this machine; the PC voice path is covered by tests with a
fake speaker, and one real spoken sentence on this computer is the remaining
manual check.

### Ekranda boshqaruv: animatsiyali sichqoncha

«Yulduz Usmonovaning qo‘shig‘ini qo‘y» — `play_youtube` qidiruv natijalari
sahifasida qoldirmaydi: YouTube'dan haqiqiy videoni topadi (bor bo‘lsa, rasmiy
klipni), `watch?v=…&autoplay=1` bilan ochadi. Ovoz masalasi shunday hal qilingan:
Chromium `--autoplay-policy=no-user-gesture-required` bayrog‘i bilan, Ksenus'ning
o‘z profili (`%LOCALAPPDATA%\\Ksenus\\Browser`) ichida ishga tushadi — shu
bayroq bilan sahifa bosishsiz **ovoz bilan** ijro etiladi. Shu sababli qo‘shiq
jim qolmaydi va hech qanday tugma bosishga bog‘liq emas. Chromium topilmasa,
eski yo‘l qoladi: ovozsiz `mute=1` sahifa va `m` tugmasi, u ham faqat brauzer
oynasi haqiqatan oldinda bo‘lganda bosiladi. Javob video nomi, kanali,
davomiyligi va havolasini aytadi.

«YouTube'ga kirib qo‘yib ber» — endi bu ekranda ko‘rinadi. `control_screen`
haqiqiy sichqoncha va klaviaturani ishlatadi, `gui_cursor_window.py` esa shu
o‘tish davomida ekran ustiga shaffof, bosishni o‘tkazib yuboradigan qatlam
chizadi:

* real ko‘rsatkich atrofida pulsatsiyalanuvchi yashil halqa va to‘rt nur,
* «Ksenus» yorlig‘i va bajarilayotgan qadam matni («…: Ochilmoqda: youtube.com»),
* har bir bosish joyida tarqaladigan to‘lqin.

Qatlam hammasi ustida turadi (`-topmost`), shuning uchun Ksenus Telegramda
ishlayotgan bo‘lsa ham, boshqa oynada bo‘lsa ham ko‘rinadi. Oxirgi amaldan
`GUI_CURSOR_IDLE_SECONDS` (6 s) o‘tgach o‘zi yopiladi.

Bir chaqiruv — bitta qisqa reja (`open`, `app`, `move`, `click`,
`double_click`, `right_click`, `type`, `key`, `scroll`, `wait`; ko‘pi bilan
12 qadam), va u darhol bajariladi: egasining so‘rovi o‘zi ruxsat
(`OWNER_AUTO_APPROVE`), har qadam uchun qayta «ha» so‘ralmaydi. «YouTube'da
qo‘shiq qo‘y»: `open https://www.youtube.com` → `key "/"` (YouTube qidiruv
maydoniga fokus beradi) → `type "qo‘shiq nomi"` → `key "enter"`. Piksel
koordinatalari kerak bo‘lsa, avval `screen_info` ekran o‘lchami va ko‘rsatkich
o‘rnini aytadi.

Faqat brauzer emas: `app` qadami (va `open_app` tooli) istalgan dasturni
nomidan ochadi — `code` (VS Code), `telegram`, `notepad`, `calc`, `explorer`,
`chrome`, `word`, `excel` va hokazo — keyin oynani oldinga chiqaradi. Ilova
allaqachon ochiq bo‘lsa, ikkinchi nusxa ochilmaydi: o‘sha oynaning o‘zi oldinga
chiqariladi. `telegram` nomi kompyuterda o‘rnatilgan mijozni topadi (Telegram
Desktop bo‘lmasa — AyuGram, 64Gram, Kotatogram), `Downloads` dagi portativ
dasturlar ham fayl nomi bo‘yicha qidiriladi. Yangi
oyna jarayon ID'si yoki sarlavhasi bo‘yicha emas, balki *paydo bo‘lgan yangi
oyna* sifatida topiladi, shuning uchun Windows 11 ning paketli ilovalari
(Kalkulyator, Notepad) ham to‘g‘ri fokus oladi. «VS Code'da kod yoz» deyilsa:
`write_file`/`edit_file` bilan fayl yoziladi, `run_command` bilan tekshiriladi,
keyin `open_app(app="code", arguments="./papka")` bilan muharrir ochiladi.

`GUI_CONTROL_ENABLED=false` ikkala toolni ham o‘chiradi. Faqat Windows uchun:
boshqa tizimda tool «ishlamaydi» deb halol javob beradi va hech narsa
qilmaydi. Holat: haqiqiy sichqoncha/klaviatura yo‘li Windowsda sinalgan
(`tests/test_gui_control.py` fake drayver bilan), ammo to‘liq «YouTube ochish»
okeani sizning kompyuteringizda bir marta qo‘lda sinalishi kerak — shundan
keyingina buni tasdiqlangan deb hisoblash mumkin.

### Uzbek voice: VoiceLab (Aisha AI)

`TTS_PROVIDER=voicelab` speaks through VoiceLab's Uzbek-native voices instead of
a multilingual voice reading Uzbek letters. The key lives in `VOICELAB_API_KEY`
(`.env`, never committed, never logged).

- `POST /v1/tts` returns a 24 kHz mono 16-bit WAV; the engine hands raw PCM to
the call path and MP3 to Telegram voice notes.
- Voices come from `GET /v1/voices?language=uz`; `VOICELAB_VOICE_ID` picks one
(default **Gulnoza**, `voice_01J9NEUTRAL0000000000000001`).
- A `202` (still generating) is polled with a bounded budget instead of hanging.
  A rejected key, exhausted credits or a missing scope is reported, not retried.
- `TTS_FALLBACK_PROVIDER=edge` keeps the keyless Uzbek Edge voice as a fallback,
so a VoiceLab outage does not silence calls.
- Transcription is unchanged (`SPEECH_PROVIDER`); VoiceLab's STT endpoint is not
wired up.

Jim qolish yo‘q. Qo‘ng‘iroqda har bir bosqich aniq xabar beradi: STT birinchi
urinishda yiqilsa (Gemini 429/503 kabi), audio yana bir marta yuboriladi;
yaqin 0.7 s dan keyin ham eshitilmasa, agent «Eshitmadim. Yana bir marta ayting,
iltimos.» deb gapiradi. Model javobi
`TELEGRAM_CALL_REPLY_DEADLINE_SECONDS` (default 9 s) ichida kelmasa yoki bo‘sh
qaytsa, agent «Kechirasiz, hozir javob tayyorlay olmadim. Yana bir marta
ayting.» deb aytadi va qo‘ng‘iroq davom etadi. Sabab: bepul model 20 soniya
«o‘ylanib» qolsa ham, odam jim liniyani eshitmasin.

Live check: `build_speech_engine(...).synthesize(..., "pcm")` returned 3.89 s of
24 kHz audio for an Uzbek sentence through VoiceLab.

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
| POST | `/images/remove-background` | Upload an image, get a transparent PNG back (ApyHub) |
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

## Computer agent

The same brain can work on the computer itself, from the terminal
(`desktop.py`) and from Telegram:

| Tool | What it does | Confirmation |
| --- | --- | --- |
| `read_file`, `list_files`, `write_file` | Read, list and write text files inside the project folder | none |
| `edit_file` | Replace one exact snippet, so the rest of the file is untouched | none |
| `move_file` | Move or rename one file; never overwrites | none |
| `delete_file` | Delete exactly one file (folders are never deleted) | none |
| `run_command` | One shell command in the project folder (cmd on Windows) | none |
| `system_info` | OS, Python, project folder, free disk, shell | none |
| `open_url` | Open a public page in the default browser | none |
| `open_app` | Start any program by name (VS Code, Telegram, Chrome, Notepad, Calculator…) and lift its window | none |
| `play_youtube` | Find the real video for a song and open it **playing with sound** (Chromium autoplay switch in a Ksenus profile; muted page + `m` key only as fallback) | none |
| `remove_background` | Cut the subject out of an image and save a transparent PNG next to it (`APY_TOKEN`; the original is never changed) | none |

Safety model:

- Everything stays inside one root (`LOCAL_FILES_ROOT`, empty = project folder).
  Absolute paths, `..`, symlinked escapes and `cd ..` are refused.
- `.env*`, `data/`, `logs/`, `.git/`, `.venv/` and key/database files are never
  readable or writable.
- Refused outright, not confirmable: `format`, `shutdown`, `diskpart`,
  `bcdedit`, recursive `del`/`rmdir`, `reg delete`, `net user`, `schtasks`,
  `git push`, `git reset --hard`, piping a download into a shell, encoded
  PowerShell, and any command that mentions `.env`.
- `OWNER_AUTO_APPROVE=true` (the default) means one permission at the start is
  enough: the owner's request is the approval, so commands, screen plans and
  file changes run in that same turn instead of stopping for a second «ha» on
  every step. Setting it to `false` brings back the old behaviour, where
  `COMPUTER_CONFIRM_COMMANDS=true` holds every non-read-only command until the
  owner replies `confirm` on Telegram, `/confirm` in `desktop.py`, or approves
  the pending action in the dashboard.
- Calls and group joins keep asking even with `OWNER_AUTO_APPROVE=true`: they
  reach another person, so the owner's own «ha» is still required.
- Commands time out (`COMPUTER_COMMAND_TIMEOUT`), are killed with their child
  process tree, and output is truncated (`COMPUTER_OUTPUT_MAX_KB`).

```powershell
.\.venv\Scripts\python.exe desktop.py
# Siz: tests/test_computer.py ni ishga tushirib, xatoni tuzat
```

One reply may use at most `AGENT_MAX_TOOL_CALLS` tool calls (default 8). When
the budget is gone the agent must answer with what it already has and say what
is left, instead of looping on a failing call. Multi-step goals are planned in
the prompt: short step list first, then one tool call per step, then an honest
report of what finished and what did not.

## Web access

| Tool | What it does | Confirmation |
| --- | --- | --- |
| `web_search` | Public search; returns titles, URLs and snippets | none |
| `web_fetch` | Read one public page as title + readable text | none |
| `open_url` | Open a public page in this computer's browser | none |

- Full web search needs one free key: `EXA_API_KEY` (https://exa.ai, credits
  refreshed monthly). The request is plain REST over httpx; no SDK.
- Without that key search still works, but only over Wikipedia (Uzbek and
  English) and DuckDuckGo's instant answer, and every reply says it is a
  limited search. Scraping public result pages (Bing, DuckDuckGo HTML) was
  tried and rejected: DuckDuckGo answers HTTP 202 with a challenge page and
  Bing returns market-localised noise, which would be presented as facts.
- Only public http(s) pages are reachable. `localhost`, 127/8, 10/8, 172.16/12,
  192.168/16, 169.254/16 (cloud metadata), `.local` / `.internal` names, bare
  single-label hostnames, URLs with a username/password and `file://` are
  refused before any request. Every redirect hop is checked again, so a public
  URL cannot bounce the agent to `127.0.0.1`.
- Downloads are bounded (`WEB_FETCH_MAX_KB`), requests time out
  (`WEB_TIMEOUT_SECONDS`), and page text is treated as data, never as
  instructions: a page that says "ignore your rules" is reported, not followed.

## Image background removal (ApyHub)

`remove_background` (and `POST /images/remove-background`) sends one image to
ApyHub's documented endpoint and returns PNG bytes with the background cut out.
The tool saves `<name>-no-bg.png` next to the original and never changes or
deletes the original; on any failure the reply says so and the original stays.

- `APY_TOKEN` from <https://apyhub.com> is the only setting. It is sent in the
  `apy-token` header, server side only, and never logged or returned. Empty =
  the answer says the token is missing and nothing is sent.
- Request: `POST https://api.eu.apyhub.com/apyhub/remove-background-from-images/multi-part/download`,
  multipart field `image`, up to 100 MB; plain REST over httpx, no SDK.
- The upload is checked first (a real PNG/JPEG/WebP/GIF/BMP/TIFF, bounded size),
  so a text file is refused instead of being billed.
- The returned PNG is decoded here and must contain real transparent pixels; an
  opaque answer is reported as a failed cutout instead of being saved. What is
  checked is transparency, not edge quality: the edges are ApyHub's
  segmentation, so the reply never promises perfect edges.

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
- Shell commands and file changes stay inside the project folder; destructive
  commands and credential files are refused before they run, and non-read-only
  commands need confirmation (`COMPUTER_CONFIRM_COMMANDS`).
- Web access is public-only: local network addresses, metadata endpoints and
  file URLs are refused, including on every redirect hop.
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
