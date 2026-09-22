import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional

from src.domain.request_context import RequestContext
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_call_buffer import VoiceCallBuffer, VoiceTurnSegment
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.alert_sink import AlertSinkPort
from src.ports.call_control_plane_port import CallControlPlanePort
from src.ports.realtime_session_port import RealtimeSessionPort
from src.utils.logger import logger

_UNKNOWN_MODEL_LABEL = "unknown"
# SPOKEN_DELIVERY's `silence` rule reacts to this exact kind of note.
_SILENCE_NOTE = "[The caller has been silent for {seconds} seconds.]"
_WATCHDOG_TICK_S = 0.25


@dataclass
class _CallState:
    """Turn state shared by the event loop and the silence watchdog of one call."""

    playback: PlaybackTracker
    response_active: bool = False
    caller_speaking: bool = False
    silence_prompted: bool = False
    item_id: Optional[str] = None
    item_start_bytes: int = 0


class VoiceSessionService:
    """The relay's session loop (RFC §4.5). Not an agent - REQ-ARCH-03/-30
    forbid naming this *Agent (RFC §4.4)."""

    def __init__(
        self,
        realtime_session_factory: Callable[[], RealtimeSessionPort],
        control_plane: CallControlPlanePort,
        alert_sink: AlertSinkPort,
        reasoning_effort: str = "medium",
        silence_timeout_s: float = 8.0,
    ) -> None:
        self._session_factory = realtime_session_factory
        self._control_plane = control_plane
        self._alert_sink = alert_sink
        self._reasoning_effort = reasoning_effort
        self._silence_timeout_s = silence_timeout_s

    async def handle_call(
        self,
        ticket: str,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        playback: PlaybackTracker,
    ) -> None:
        """`playback` is fed by the transport (bytes sent, marks echoed back); this
        loop only reads it."""
        config = await self._control_plane.fetch_session_config(ticket)
        state = _CallState(playback=playback)
        async with RequestContext(user_id=config["user_id"], account_id=config["account_id"]):
            await self._run_call(ticket, config, inbound_audio, send_outbound_audio, clear_outbound_audio, state)

    async def _run_call(
        self,
        ticket: str,
        config: dict,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        state: _CallState,
    ) -> None:
        session = self._session_factory()
        buffer = VoiceCallBuffer(call_id=ticket)
        forward_task: Optional[asyncio.Task] = None
        consume_task: Optional[asyncio.Task] = None
        watchdog_task: Optional[asyncio.Task] = None

        try:
            # open() is inside the try/finally, not before it: a failed open (e.g. a
            # transient connection error to the provider) must still reach close()
            # and submit_transcript() below - otherwise the relay's one-call-per-user
            # marker (RFC §3) only releases via TTL instead of immediately, and the
            # failure leaves no record on the main-service side.
            try:
                await session.open(instructions=config["instructions"], reasoning_effort=self._reasoning_effort, tools=[])
            except Exception as exc:
                logger.error(f"voice call {ticket}: failed to open realtime session: {exc}")
                raise

            # Inbound forwarding and outbound event consumption are two independent
            # streams (inbound audio keeps arriving from the caller regardless of
            # provider turn boundaries) - they must run as real concurrent tasks,
            # not a fire-and-forget task raced against a synchronous loop. A bare
            # `asyncio.ensure_future(...)` followed by an immediate `.cancel()` on
            # loop exit never gives the forwarding task a chance to actually run
            # if nothing in the receive loop truly suspends the event loop first -
            # asyncio.wait() below is what forces that handoff.
            forward_task = asyncio.ensure_future(self._forward_inbound(session, inbound_audio))
            consume_task = asyncio.ensure_future(
                self._consume_events(ticket, session, buffer, send_outbound_audio, clear_outbound_audio, state)
            )
            # The watchdog never ends on its own; the call ends with the audio streams.
            watchdog_task = asyncio.ensure_future(self._watch_silence(session, state))
            await asyncio.wait({forward_task, consume_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            tasks = [task for task in (forward_task, consume_task, watchdog_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    # Logged, not re-raised: this is a top-level call loop with no
                    # caller to propagate to - the call simply ends here, which is
                    # the correct outcome for a crashed forward/consume loop. The
                    # "error" event path above already alerts on provider-side
                    # failures; this only catches an unexpected bug in the loop
                    # itself, and still needs to be visible in logs.
                    if isinstance(result, Exception):
                        logger.error(f"voice call {ticket}: session loop crashed: {result}")
            await session.close()
            await self._control_plane.submit_transcript(
                call_id=ticket,
                user_id=config["user_id"],
                account_id=config["account_id"],
                buffer=buffer,
            )

    async def _consume_events(
        self,
        ticket: str,
        session: RealtimeSessionPort,
        buffer: VoiceCallBuffer,
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        state: _CallState,
    ) -> None:
        turn_start: Optional[datetime] = None
        pending_request_text = ""
        pending_response_text = ""
        async for event in session.receive_events():
            if event.type == "response_created":
                turn_start = datetime.now(timezone.utc)
                state.response_active = True
            elif event.type == "speech_started":
                state.caller_speaking = True
                state.silence_prompted = False
                # The caller started talking over Lelik (RFC §4.7; mechanism validated in
                # scripts/voice/test_mulaw_relay_poc.py:154-170). Barge-in is ours alone -
                # the provider's auto-interrupt is off (OpenAIRealtimeAdapter._TURN_DETECTION).
                # Guarded on response_active: response.cancel with nothing in flight is a
                # provider error, which would end the call.
                if state.response_active:
                    await self._barge_in(session, state, clear_outbound_audio)
            elif event.type == "speech_stopped":
                state.caller_speaking = False
            elif event.type == "audio_delta":
                item_id = event.payload.get("item_id")
                if item_id != state.item_id:
                    state.item_id = item_id
                    state.item_start_bytes = state.playback.sent_bytes
                await send_outbound_audio(event.payload["frame"])
            elif event.type == "user_transcript":
                pending_request_text += event.payload["text"]
            elif event.type == "model_transcript":
                pending_response_text += event.payload["text"]
            elif event.type == "response_done":
                state.response_active = False
                usage = event.payload.get("usage", {})
                if usage:
                    # The provider labels its own usage - VoiceSessionService is
                    # provider-agnostic (RealtimeSessionPort), so it must not
                    # hardcode a specific model string (REQ-ARCH-01 layer rule:
                    # provider-specific model ids live in adapters/, not services/).
                    buffer.add_usage(event.payload.get("model", _UNKNOWN_MODEL_LABEL), **usage)
                buffer.add_turn(VoiceTurnSegment(
                    request_text=pending_request_text,
                    response_text=pending_response_text,
                    started_at=turn_start or datetime.now(timezone.utc),
                    ended_at=datetime.now(timezone.utc),
                ))
                pending_request_text = ""
                pending_response_text = ""
            elif event.type == "error":
                logger.error(f"voice call {ticket}: provider error {event.payload.get('message')}")
                await self._alert_sink.post(f"Voice call {ticket} provider error: {event.payload.get('message')}")
                return

    async def _barge_in(
        self, session: RealtimeSessionPort, state: _CallState,
        clear_outbound_audio: Callable[[], Awaitable[None]],
    ) -> None:
        # Order matters. Read what was heard BEFORE clearing: Twilio echoes the marks of
        # dropped audio after a clear, which would count unheard audio as played. Then
        # drain both buffers - Twilio's queued playback and the provider's generation -
        # and cut the item to the heard part, so the model resumes from where it was
        # actually interrupted instead of believing it finished the reply.
        heard_ms = state.playback.played_ms_since(state.item_start_bytes)
        await clear_outbound_audio()
        await session.cancel_response()
        state.response_active = False
        if state.item_id:
            await session.truncate(state.item_id, heard_ms)

    async def _watch_silence(self, session: RealtimeSessionPort, state: _CallState) -> None:
        # The provider only speaks when a turn ends, so it cannot notice a caller who
        # has gone quiet (and its idle_timeout_ms is server_vad-only). Silence counts
        # from the moment Lelik's audio has finished PLAYING, not finished generating.
        # One prompt per silence: re-armed only by the caller speaking again.
        loop = asyncio.get_running_loop()
        quiet_since = loop.time()
        while True:
            await asyncio.sleep(_WATCHDOG_TICK_S)
            now = loop.time()
            if state.response_active or state.caller_speaking or not state.playback.caught_up:
                quiet_since = now
                continue
            if state.silence_prompted or now - quiet_since < self._silence_timeout_s:
                continue
            state.silence_prompted = True
            await session.submit_message(
                "system", _SILENCE_NOTE.format(seconds=round(self._silence_timeout_s)),
            )
            await session.request_response()

    async def _forward_inbound(self, session: RealtimeSessionPort, inbound_audio: AsyncIterator[AudioFrame]) -> None:
        async for frame in inbound_audio:
            await session.send_audio(frame)
