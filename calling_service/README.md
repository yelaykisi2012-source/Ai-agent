# Telegram calling: what is real, what is not

This folder documents the one-to-one Telegram call path. It is deliberately
honest about the boundary between signaling and audio.

## Status on this machine

| Capability | Status |
| --- | --- |
| Telethon user-account login, messaging | Implemented, runs locally |
| Incoming call detection (`updatePhoneCall`) | Implemented |
| Declining an incoming call via `phone.discardCall` | Implemented |
| Call history + audit records | Implemented |
| Outgoing call | **Refused on purpose** |
| Live two-way call audio | **Not enabled** |
| `ntgcalls` native transport installed | Yes, 3.0.7 (Windows x86_64, cp314) |

`GET /calls/diagnostics` returns the live state, including the native library
version and its protocol layers.

## Why audio is not simply "on"

A Telegram 1:1 call is three separate problems:

1. **Signaling** — `phone.requestCall`, `phone.acceptCall`,
   `phone.confirmCall`, `phone.sendSignalingData`, `updatePhoneCall`.
   Telethon carries this.
2. **Key exchange** — Diffie-Hellman with `g_a_hash` / `g_a_or_b`, a key
   fingerprint and a four-emoji verification hash.
3. **Media** — the custom end-to-end encrypted UDP transport described in
   Telegram's "End-to-End Encrypted Voice Calls" document: keys derived from
   the DH exchange, per-packet MTProto-style encryption, relay ("reflector")
   servers, Opus frames, and a jitter buffer. Telethon does not implement this
   and its own documentation says streaming audio is beyond its scope.

`ntgcalls` implements step 3: it is a native C++/WebRTC library with prebuilt
Windows wheels, private-call support and raw PCM frames.

## Verified `ntgcalls` 3.0.7 API surface

These methods were confirmed by introspecting the installed wheel, not from
memory:

```
NTgCalls()
  .create_p2p_call(user_id)
  .init_exchange(user_id, dh_config, ga_hash)
  .exchange_keys(user_id, g_a_or_b, fingerprint)
  .connect_p2p(user_id, servers, versions, p2p_allow, udp_p2p, udp_reflector)
  .send_signaling_data(chat_id, msg_key)
  .on_signaling_data(callback(user_id, data))
  .set_stream_sources(chat_id, mode, media)
  .send_external_frame(chat_id, device, data, frame_data)
  .on_frames(callback(chat_id, mode, device, frames))
  .pause / .resume / .mute / .unmute / .stop
  .get_state / .get_call_type / .get_protocol
```

Supporting types: `DhConfig`, `AuthParams`, `RTCServer`, `Protocol`,
`AudioDescription`, `MediaDescription`, `FrameData`, `StreamMode`,
`StreamDevice`.

## The setup ordering that matters

A public, working proof-of-concept (NovaMind Studio, AGPL-3.0) reports that
inbound audio for private calls only arrives with this order:

1. `create_p2p_call`
2. set the **CAPTURE** stream source (before the key exchange)
3. `connect_p2p`
4. set **PLAYBACK** on the microphone slot (inbound frames arrive as
   `device=MICROPHONE, mode=PLAYBACK`)
5. `unmute`
6. `resume`
7. keep a continuous outbound frame stream (silence when idle)

Frame contract: 48 kHz mono PCM int16 little-endian, 10 ms frames (480 samples,
960 bytes). Sending 20 ms frames causes underruns.

## Licensing

- `ntgcalls` is a separate native dependency; check its repository `LICENSE`
  before redistributing it.
- The public proof-of-concept above is **AGPL-3.0**. Its code is not copied
  into this project. If you reuse it, its copyleft terms apply to any
  network-accessible service built from it.
- Telegram's API terms apply to the user account. Ksenus only calls the one
  authorized owner and never performs bulk or unsolicited calling.

## What finishing this requires

1. Two Telegram accounts that can call each other; the Ksenus account must not
   be your primary account.
2. `api_id` / `api_hash` from <https://my.telegram.org/apps>, then
   `python -m telegram.authentication login`.
3. A transport integration that performs the DH exchange and relays signaling
   both ways, plus a frame pump at the contract above.
4. A live verification: call in, speak, hear the reply, interrupt, hang up, and
   confirm no native process is left behind.

Until steps 3 and 4 are done, `CallManager` declines incoming calls with a
clear reason and refuses outgoing calls rather than connecting a silent call.
