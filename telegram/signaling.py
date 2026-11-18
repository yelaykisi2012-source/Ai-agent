"""MTProto call signaling helpers.

Telegram's one-to-one calls are end-to-end encrypted and negotiated through
`phone.*` methods plus `updatePhoneCall` updates. Telethon can carry that
signaling but not the media; media needs the WebRTC transport in
``telegram.media``. This module classifies call updates and builds the
discard requests used when a call cannot be carried.

Nothing here pretends to answer a call that has no audio path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Constructor names from telegram.tl.types that we care about.
INCOMING_STATES = {
    "PhoneCallRequested": "ringing",
}
KNOWN_STATES = {
    "PhoneCallRequested": "ringing",
    "PhoneCallWaiting": "waiting",
    "PhoneCallAccepted": "accepted",
    "PhoneCall": "confirmed",
    "PhoneCallDiscarded": "discarded",
}


@dataclass(frozen=True)
class CallEvent:
    """A normalized view of one MTProto call update."""

    call_id: int
    direction: str
    state: str
    peer_id: int | None
    access_hash: int | None
    admin_id: int | None
    participant_id: int | None
    created_at: datetime


def constructor_name(value: object) -> str:
    """Return the Telegram constructor name for a TL object."""
    return type(value).__name__


def classify_call_update(update: object) -> CallEvent | None:
    """Turn an ``UpdatePhoneCall`` into a :class:`CallEvent`.

    Returns None for updates that are not about a call. Values are read
    defensively because constructors differ across Telegram layers.
    """
    if constructor_name(update) != "UpdatePhoneCall":
        return None

    call = getattr(update, "phone_call", None)
    if call is None:
        return None

    name = constructor_name(call)
    state = KNOWN_STATES.get(name)
    if state is None:
        logger.debug("Ignoring unsupported call constructor %s", name)
        return None

    call_id = getattr(call, "id", None)
    if call_id is None:
        return None

    admin_id = getattr(call, "admin_id", None)
    participant_id = getattr(call, "participant_id", None)
    access_hash = getattr(call, "access_hash", None)

    # A requested call is incoming when we are the participant, not the admin.
    direction = (
        "incoming"
        if state in {"ringing", "waiting"} and admin_id != participant_id
        else "outgoing"
    )

    return CallEvent(
        call_id=int(call_id),
        direction=direction,
        state=state,
        peer_id=int(participant_id) if participant_id is not None else None,
        access_hash=int(access_hash) if access_hash is not None else None,
        admin_id=int(admin_id) if admin_id is not None else None,
        participant_id=int(participant_id) if participant_id is not None else None,
        created_at=datetime.now(timezone.utc),
    )


def build_decline_request(
    call_id: int,
    access_hash: int,
    duration: int = 0,
    *,
    busy: bool = False,
):
    """Build a ``phone.discardCall`` request for one call.

    ``discardCall`` takes an ``InputPhoneCall`` (the call id plus its access
    hash), not a bare user id; passing a user id is rejected by Telegram.
    """
    from telethon.tl import functions, types

    reason_class = (
        types.PhoneCallDiscardReasonBusy if busy else types.PhoneCallDiscardReasonMissed
    )
    peer = types.InputPhoneCall(id=int(call_id), access_hash=int(access_hash))
    return functions.phone.DiscardCallRequest(
        peer=peer,
        duration=duration,
        reason=reason_class(),
        connection_id=0,
        video=False,
    )


async def decline_call(client: object, event: CallEvent, *, busy: bool = False) -> bool:
    """Decline an incoming call through MTProto. Returns success."""
    if event.access_hash is None:
        logger.warning("Cannot decline call %s: no access hash", event.call_id)
        return False
    try:
        request = build_decline_request(event.call_id, event.access_hash, busy=busy)
        await client(request)  # type: ignore[operator]
        return True
    except Exception:  # noqa: BLE001 - remote failures must not crash the client
        logger.exception("Failed to decline call %s", event.call_id)
        return False


def build_outgoing_call_request(peer_id: int, protocol: object, random_id: int, g_a_hash: bytes):
    """Build ``phone.requestCall`` for a peer.

    Only the signaling request is built here. Placing a call without a media
    transport would ring the user and then fail, so the caller must check the
    transport first.
    """
    from telethon.tl import functions

    return functions.phone.RequestCallRequest(
        user_id=peer_id,
        random_id=random_id,
        g_a_hash=g_a_hash,
        protocol=protocol,
        video=False,
    )
