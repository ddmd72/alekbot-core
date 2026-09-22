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
from src.domain.voice_playback_tracker import PlaybackTracker
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
        playback = PlaybackTracker()

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
            # Twilio echoes a mark once the audio queued before it has played - the
            # only signal of what the caller actually heard (PlaybackTracker).
            await ws.send(json.dumps({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": playback.record_sent(frame.payload)},
            }))

        async def clear_outbound_audio() -> None:
            # Barge-in half that Twilio owns: drop every media frame already queued
            # for playback on its side. Cancelling the provider's response alone is
            # not enough - audio sent before the interrupt keeps playing out over
            # the caller's voice (validated in scripts/voice/test_mulaw_relay_poc.py).
            if stream_sid is None:
                return
            await ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))

        # Cleanup (push the sentinel, await call_task) MUST run on every exit path
        # from the loop below, not just the explicit "stop" branch: websockets==15.0.1's
        # Connection.__aiter__ swallows ConnectionClosedOK internally and just returns
        # (websockets/asyncio/connection.py:228-242) — a CLEAN close with no "stop"
        # event ends the `async for raw in ws:` loop with no exception raised at all,
        # so an `except` block alone never fires for that case. A single `finally`
        # wrapping the whole loop covers all three exits uniformly: normal fallthrough
        # (clean close, no "stop"), the explicit "stop" break, and any exception —
        # without needing the "stop" branch (or an except branch) to duplicate the
        # cleanup itself. Guarded on `call_task is not None` (loop may end before any
        # "start" event ever arrived); awaiting an already-done task a second time is
        # not a risk here since cleanup now only ever happens once, in this block.
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
                            clear_outbound_audio=clear_outbound_audio,
                            playback=playback,
                        )
                    )
                elif event == "media":
                    await inbound_queue.put(AudioFrame(
                        encoding="audio/pcmu",
                        sample_rate_hz=8000,
                        payload=msg["media"]["payload"],
                        track="inbound",
                    ))
                elif event == "mark":
                    playback.record_played(msg["mark"]["name"])
                elif event == "stop":
                    break
        except Exception:
            logger.error(f"media stream for ticket={ticket} failed", exc_info=True)
            raise
        finally:
            await inbound_queue.put(None)
            if call_task is not None:
                await call_task
            logger.info(f"media stream closed for ticket={ticket}")
