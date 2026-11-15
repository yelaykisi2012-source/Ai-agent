"""Browser voice loop over WebSocket.

The browser records with MediaRecorder and streams chunks; on the "end"
message the server transcribes, asks the brain, synthesises the reply and
returns the text plus MP3 bytes. This is turn-based streaming rather than
full-duplex barge-in: the user speaks, then the agent answers. That limit is
stated in the UI and documentation instead of being hidden.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from anyio import to_thread
from fastapi import WebSocket, WebSocketDisconnect

import memory
from brain import AgentBrain, BrainError
from config import Settings
from voice.audio import AudioError, ensure_upload
from voice.speech_engine import SpeechEngine, VoiceError

logger = logging.getLogger(__name__)

_MAX_TURNS = 50


async def serve_voice_session(
    websocket: WebSocket,
    engine: SpeechEngine,
    brain: AgentBrain,
    settings: Settings,
) -> None:
    """Run one voice conversation until the client disconnects."""
    await websocket.accept()
    conversation_id: int | None = None
    buffer = bytearray()
    turns = 0

    try:
        while turns < _MAX_TURNS:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return

            chunk = message.get("bytes")
            if chunk:
                buffer.extend(chunk)
                if len(buffer) > settings.voice_max_upload_mb * 1024 * 1024:
                    await websocket.send_json(
                        {"type": "error", "detail": "Recording is too long."}
                    )
                    buffer.clear()
                continue

            raw = message.get("text")
            if not raw:
                continue
            try:
                control = json.loads(raw)
            except ValueError:
                await websocket.send_json(
                    {"type": "error", "detail": "Invalid control message."}
                )
                continue

            action = control.get("type")
            if action == "cancel":
                buffer.clear()
                await websocket.send_json({"type": "cancelled"})
                continue

            if action != "end":
                continue

            if not buffer:
                await websocket.send_json(
                    {"type": "error", "detail": "No audio was received."}
                )
                continue

            audio = bytes(buffer)
            buffer.clear()
            turns += 1

            try:
                ensure_upload("utterance.webm", audio, settings.voice_max_upload_mb)
                transcript = await engine.transcribe(audio, filename="utterance.webm")
            except (AudioError, VoiceError) as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                continue

            transcript = transcript.strip()
            if not transcript:
                await websocket.send_json(
                    {"type": "no_speech", "detail": "I did not catch any speech."}
                )
                continue

            if conversation_id is None:
                conversation = await to_thread.run_sync(
                    memory.create_conversation, transcript
                )
                conversation_id = conversation.id
                history: list[dict[str, str]] = []
            else:
                messages = await to_thread.run_sync(
                    memory.recent_messages,
                    conversation_id,
                    settings.chat_context_messages,
                )
                history = [
                    {"role": item.role, "content": item.content} for item in messages
                ]

            await to_thread.run_sync(
                memory.add_message, conversation_id, "user", transcript
            )
            await websocket.send_json(
                {"type": "transcript", "text": transcript, "conversation_id": conversation_id}
            )

            try:
                reply = await brain.reply(history=history, user_message=transcript)
            except BrainError as exc:
                await websocket.send_json(
                    {"type": "error", "detail": exc.public_message}
                )
                continue

            await to_thread.run_sync(
                memory.add_message, conversation_id, "assistant", reply.text
            )
            await websocket.send_json(
                {
                    "type": "reply",
                    "text": reply.text,
                    "conversation_id": conversation_id,
                    "tools": [tool.to_payload() for tool in reply.tool_results],
                }
            )

            try:
                audio_reply = await engine.synthesize(reply.text)
            except (VoiceError, ValueError) as exc:
                logger.warning("TTS failed: %s", exc)
                await websocket.send_json(
                    {"type": "tts_error", "detail": "The text reply could not be spoken."}
                )
                continue

            await websocket.send_bytes(audio_reply)

        await websocket.send_json(
            {"type": "error", "detail": "This voice session reached its turn limit."}
        )
    except WebSocketDisconnect:
        logger.info("Voice session disconnected")
    except Exception:  # noqa: BLE001 - never leave the socket hanging
        logger.exception("Voice session crashed")
        try:
            await websocket.send_json(
                {"type": "error", "detail": "The voice session failed."}
            )
        except Exception:  # pragma: no cover - socket already gone
            pass
    finally:
        try:
            await websocket.close()
        except Exception:  # pragma: no cover - already closed
            pass


def describe_protocol() -> dict[str, Any]:
    """Machine-readable description of the WebSocket protocol."""
    return {
        "send": "binary audio chunks, then {\"type\": \"end\"}",
        "receive": [
            {"type": "transcript", "text": "..."},
            {"type": "reply", "text": "..."},
            {"type": "no_speech"},
            {"type": "error", "detail": "..."},
        ],
        "audio_reply": "MP3 bytes after a reply message",
    }
