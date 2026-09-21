"""
Twilio Media Streams WebSocket handler — relay-side entrypoint into
VoiceSessionService (Task 12).

REQ-ARCH-25: handlers/ must not import ports/ directly. This handler only
knows about `AudioFrame` (domain) and `VoiceSessionService` (service) — never
`RealtimeSessionPort` or `CallControlPlanePort`.

Twilio's Media Streams wire protocol carries audio already base64-encoded
mulaw (audio/x-mulaw, 8kHz) inside each `media` event's `payload` field.
`AudioFrame.payload` holds that base64 string as-is (see
`OpenAIRealtimeAdapter.send_audio`'s docstring for the same convention on the
provider side) — this handler neither decodes nor re-encodes it, only carries
it between the two WebSocket legs (Twilio <-> VoiceSessionService).
"""
import asyncio
import json
from typing import AsyncIterator

from src.domain.voice_audio_frame import AudioFrame
from src.services.voice_session_service import VoiceSessionService
from src.utils.logger import logger


class MediaStreamHandler:
    """One Twilio Media Stream WebSocket connection, relay-side. Extracts the
    call ticket from the `start` event's `customParameters` and drives
    `VoiceSessionService.handle_call` for the lifetime of the stream."""

    def __init__(self, session_service: VoiceSessionService) -> None:
        self._session_service = session_service

    async def handle_connection(self, ws) -> None:
        stream_sid = None
        ticket = None
        inbound_queue: "asyncio.Queue" = asyncio.Queue()

        async def inbound_frames() -> AsyncIterator[AudioFrame]:
            while True:
                frame = await inbound_queue.get()
                if frame is None:
                    return
                yield frame

        async def send_outbound(frame: AudioFrame) -> None:
            if stream_sid is None:
                return
            await ws.send(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": frame.payload},
            }))

        call_task: "asyncio.Future | None" = None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "start":
                    stream_sid = msg["start"]["streamSid"]
                    ticket = msg["start"]["customParameters"]["ticket"]
                    call_task = asyncio.ensure_future(
                        self._session_service.handle_call(
                            ticket=ticket,
                            inbound_audio=inbound_frames(),
                            send_outbound_audio=send_outbound,
                        )
                    )
                elif event == "media":
                    await inbound_queue.put(AudioFrame(
                        encoding="audio/pcmu",
                        sample_rate_hz=8000,
                        payload=msg["media"]["payload"],
                        track="inbound",
                    ))
                elif event == "stop":
                    await inbound_queue.put(None)
                    if call_task is not None:
                        await call_task
                    break
        except Exception:
            logger.error(f"media stream for ticket={ticket} failed", exc_info=True)
            # Unblock inbound_frames()/handle_call() so a mid-stream WebSocket
            # error (e.g. Twilio dropping the connection without a "stop"
            # event) doesn't leave call_task running forever against a dead
            # connection.
            await inbound_queue.put(None)
            if call_task is not None:
                await call_task
            raise
        finally:
            logger.info(f"media stream closed for ticket={ticket}")
