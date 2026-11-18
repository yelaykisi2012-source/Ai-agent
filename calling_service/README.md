# Telegram calling: what is real, what is not

This folder documents the one-to-one Telegram call path.

## Status on this machine

| Capability | Status |
| --- | --- |
| Telethon user-account login, messaging | Implemented, verified locally |
| `ntgcalls 3.0.7` native wheel (Windows, cp314) | Loads and answers (`/calls/diagnostics`) |
| Outgoing call signaling (`phone.requestCall` → `confirmCall`) | Implemented |
| Incoming call (`updatePhoneCall` → `phone.acceptCall`) | Implemented |
| DH key exchange `g_a_hash` / `g_b` / `g_a` + fingerprint | Implemented through ntgcalls |
| Signaling relay (`updatePhoneCallSignalingData` ↔ `sendSignalingData`) | Implemented |
| 48 kHz mono 10 ms PCM in / out | Implemented, unit-tested |
| VAD, barge-in, ring/connect/duration timeouts | Implemented, unit-tested |
| STT → brain → TTS voice loop inside a call | Implemented, unit-tested with fakes |
| Connection-loss grace + clean teardown | Implemented, unit-tested |
| **Live two-account call** | **Not yet verified on this machine** |

`GET /calls/status` shows whether a call is live, who may be called and whether
the native transport is available. `GET /calls/diagnostics` shows the library
version, protocol layers and the audio devices Windows exposes.

## Architecture

```
Telegram MTProto  ── Telethon ──► telegram/signaling.py   (classify updates)
                                   telegram/voice_call.py  (one call session)
                                        │  requestCall / acceptCall /
                                        │  confirmCall / discardCall /
                                        │  sendSignalingData
                                        ▼
                                   telegram/media.py       (ntgcalls 3.x)
                                        │  create_p2p_call, init_exchange,
                                        │  exchange_keys, connect_p2p,
                                        │  set_stream_sources, send_external_frame
                                        ▼
                              WebRTC/SRTP encrypted UDP voice
```

Audio contract: **48 kHz mono signed-16-bit little-endian PCM, 10 ms frames
(480 samples, 960 bytes)**. Outbound speech is synthesised at 24 kHz by the
speech engine and doubled to 48 kHz; inbound frames are wrapped in a WAV
container for transcription.

## Verified `ntgcalls` 3.0.7 API

Introspected from the installed wheel, not from memory:

```
NTgCalls()
  .create_p2p_call(user_id)
  .init_exchange(user_id, DhConfig(g, p, random), ga_hash)   # -> g_a_hash or g_b
  .exchange_keys(user_id, g_a_or_b, fingerprint)             # -> AuthParams
  .connect_p2p(user_id, servers, versions, p2p_allowed, custom_parameters)
  .send_signaling_data(chat_id, data)
  .on_signaling_data(callback(chat_id, data))
  .set_stream_sources(chat_id, StreamMode.CAPTURE|PLAYBACK, MediaDescription(...))
  .send_external_frame(chat_id, StreamDevice.MICROPHONE, pcm, FrameData(...))
  .on_frames(callback(chat_id, mode, device, frames))
  .pause / .resume / .mute / .unmute / .stop / .get_state / .get_protocol
```

Notes that cost real time to establish:

* every call action returns an awaitable and must be awaited;
* `MediaDescription` takes **four positional** arguments
  `(microphone, speaker, camera, screen)`; keyword-only use raises `TypeError`;
* `connect_p2p` in 3.x takes **five** arguments; older documentation showing a
  seventh pair (`udp_p2p`, `udp_reflector`) is wrong - those come from
  `get_protocol()`;
* `phone.discardCall` needs an `InputPhoneCall(id, access_hash)`, not a user id;
* relay `PhoneConnection`s authenticate with a peer tag and are always TURN;
  `PhoneConnectionWebrtc` keeps its own username/password.

## Configuration

```ini
TELEGRAM_ENABLED=true
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
# Your personal account (NOT the Ksenus account) - required before any call.
TELEGRAM_OWNER_USER_ID=123456789
# Live audio stays off until you deliberately enable it.
TELEGRAM_CALLS_ENABLED=false
TELEGRAM_CALL_AUTO_ANSWER=true
TELEGRAM_CALL_MAX_SECONDS=900
TELEGRAM_CALL_RING_TIMEOUT_SECONDS=45
TELEGRAM_CALL_CONNECT_TIMEOUT_SECONDS=30
TELEGRAM_CALL_RECONNECT_GRACE_SECONDS=20
TELEGRAM_CALL_SILENCE_MS=700
TELEGRAM_CALL_MIN_SPEECH_MS=300
TELEGRAM_CALL_ENERGY_THRESHOLD=400
```

## Safety rules enforced in code

* Ksenus calls **only** `telegram_owner_user_id`; it refuses to dial itself.
* An outgoing call requires a confirmed `initiate_telegram_call` action; the
  model can never dial on its own.
* A second call is never started while one is live; a second incoming call is
  declined as busy.
* Incoming calls are accepted only from the owner, and only while
  `TELEGRAM_CALLS_ENABLED=true`; otherwise they are declined with a reason.
* Call start, end, timeouts and refusals are written to the audit log and the
  `CallRecord` table.
* Phone numbers, login codes, 2FA passwords and session data are never logged.

## PowerShell commands

```powershell
cd "F:\Vs code\Ai agent"

# 1. Connect the account (once)
.\.venv\Scripts\python.exe -m telegram.authentication status

# 2. Check the native transport and the devices Windows exposes
.\.venv\Scripts\python.exe -c "import asyncio,ntgcalls; r=ntgcalls.NTgCalls(); print(r.ping())"

# 3. Start the server
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000

# 4. Inspect call state
Invoke-RestMethod http://127.0.0.1:8000/calls/status | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/calls/diagnostics | ConvertTo-Json -Depth 5

# 5. Run the suite
.\.venv\Scripts\python.exe -m pytest -q
```

## What is still required before claiming "calling works"

1. A **second** Telegram account: Ksenus must run as one account and call your
   personal account. On this machine both the session and the stored owner id
   point at the same account, so `place()` refuses (dialling itself is
   pointless and spam-like).
2. Set `TELEGRAM_OWNER_USER_ID` to the personal account's numeric id, then run
   `python -m telegram.authentication login` for the Ksenus account again if
   the session was created for the personal one.
3. Set `TELEGRAM_CALLS_ENABLED=true` and restart.
4. Live verification: call in, speak, hear the reply, interrupt it, hang up,
   and confirm no native call stays registered (`/calls/status` must show
   `active: false`).
5. Optional hardening: tune `TELEGRAM_CALL_ENERGY_THRESHOLD` for your
   microphone and `TELEGRAM_CALL_SILENCE_MS` for your speaking pace.

Until steps 1-4 are done, the calling feature must be described as
"implemented and unit-tested", never as "verified on a live call".

## Licensing

* `ntgcalls` is a separate native dependency; check its `LICENSE` before
  redistributing it.
* Telegram's API terms apply to the user account. Ksenus only calls the one
  configured owner and never performs bulk or unsolicited calling.
