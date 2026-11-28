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

from src.domain.log_redaction import redact_traceback
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_playback_tracker import PlaybackTracker
from src.services.voice_session_service import VoiceSessionService
from src.domain.secret_digest import fingerprint
from src.handlers.sfu_stream_handler import is_ticket_shaped
from src.utils.logger import logger

# Twilio sends `start` right after the upgrade; a socket that has not sent a valid one by then is closed.
START_TIMEOUT_S = 5.0
# Sockets that have not yet sent a valid `start` (the phone path's counterpart of `_MAX_UNAUTHENTICATED` in
# `call_control_handler.py`).
_MAX_UNSTARTED = 32
_CLOSE_POLICY = 1008
_CLOSE_TRY_LATER = 1013


class MediaStreamHandler:
    """One Twilio Media Stream WebSocket connection, relay-side. Extracts the
    call ticket from the `start` event's `customParameters` and drives
    `VoiceSessionService.handle_call` for the lifetime of the stream."""

    def __init__(self, session_service: VoiceSessionService, *, start_timeout_s: float = START_TIMEOUT_S,
                 max_unstarted: int = _MAX_UNSTARTED) -> None:
        self._session_service = session_service
        self._start_timeout_s = start_timeout_s
        self._max_unstarted = max_unstarted
        self._unstarted = 0

    @staticmethod
    async def _close(ws, code: int) -> None:
        close = getattr(ws, "close", None)
        if close is None:
            return
        try:
            await asyncio.wait_for(close(code), timeout=2.0)
        except Exception:
            pass

    async def handle_connection(self, ws) -> None:
        if self._unstarted >= self._max_unstarted:
            logger.warning(f"media stream: rejected a socket, {self._unstarted} already waiting for start")
            await self._close(ws, _CLOSE_TRY_LATER)
            return
        self._unstarted += 1
        waiting = True

        def leave_waiting() -> None:
            nonlocal waiting
            if waiting:
                waiting = False
                self._unstarted -= 1

        try:
            await self._serve(ws, leave_waiting)
        finally:
            leave_waiting()

    async def _serve(self, ws, leave_waiting) -> None:
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

        async def send_cue(frame: AudioFrame) -> None:
            # No mark: the thinking cue is filler, not speech the caller "heard" - it must not
            # hold PlaybackTracker behind, or the silence watchdog would never see playback end.
            if stream_sid is None:
                return
            await ws.send(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": frame.payload},
            }))

        async def clear_outbound_audio() -> None:
            # Barge-in half that Twilio owns: drop every media frame already queued
            # for playback on its side. Cancelling the provider's response alone is
            # not enough - audio sent before the interrupt keeps playing out over
            # the caller's voice (validated in scripts/voice/test_mulaw_relay_poc.py).
            if stream_sid is None:
                return
            await ws.send(json.dumps({"event": "clear", "streamSid": stream_sid}))

        async def run_call(call_ticket: str) -> None:
            try:
                await self._session_service.handle_call(
                    ticket=call_ticket,
                    inbound_audio=inbound_frames(),
                    send_outbound_audio=send_outbound,
                    clear_outbound_audio=clear_outbound_audio,
                    playback=playback,
                    send_cue_audio=send_cue,
                )
            finally:
                # A session that ends on its own (silence hang-up, provider failure) must end
                # the call too: closing the stream ends Twilio's <Connect>, and with no TwiML
                # after it Twilio hangs up. After a normal hang-up the socket is already closed.
                close = getattr(ws, "close", None)
                if close is not None:
                    await close()

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

        async def frames() -> AsyncIterator:
            # A valid `start` must arrive within `start_timeout_s` of the upgrade, whatever else is sent meanwhile
            # (a socket that never speaks, or only sends other frames, is closed); after it the stream lasts as long
            # as the call.
            iterator = ws.__aiter__()
            deadline = asyncio.get_running_loop().time() + self._start_timeout_s
            while True:
                try:
                    if call_task is None:
                        remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
                        raw = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
                    else:
                        raw = await iterator.__anext__()
                except StopAsyncIteration:
                    return
                yield raw

        try:
            async for raw in frames():
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
                event = msg.get("event") if isinstance(msg, dict) else None
                if event == "start":
                    if call_task is not None:
                        continue  # a second `start` never replaces the running call
                    start = msg.get("start")
                    params = start.get("customParameters") if isinstance(start, dict) else None
                    candidate = params.get("ticket") if isinstance(params, dict) else None
                    sid = start.get("streamSid") if isinstance(start, dict) else None
                    if not isinstance(candidate, str) or not is_ticket_shaped(candidate) or not isinstance(sid, str):
                        logger.warning("media stream: start without a well-formed ticket, closed")
                        await self._close(ws, _CLOSE_POLICY)
                        return
                    stream_sid = sid
                    ticket = candidate
                    leave_waiting()
                    call_task = asyncio.ensure_future(run_call(ticket))
                elif event == "media":
                    if call_task is None:
                        continue  # audio before `start` is dropped, never queued
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
        except asyncio.TimeoutError:
            logger.info(f"media stream: no start within {self._start_timeout_s:.0f}s, closed")
            await self._close(ws, _CLOSE_POLICY)
        except Exception as exc:
            # A redacted traceback instead of exc_info: the error may come from the session open, whose text can
            # carry a URL, a header or a call's token (LELIK_VOICE_MCP_RFC §5.4).
            logger.error(f"media stream for ticket={fingerprint(ticket or '')} failed: {redact_traceback(exc)}")
            raise
        finally:
            await inbound_queue.put(None)
            if call_task is not None:
                await call_task
            logger.info(f"media stream closed for ticket={fingerprint(ticket or '')}")
