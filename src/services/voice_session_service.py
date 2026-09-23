import asyncio
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, List, Optional, Set, Tuple

from src.domain.llm import build_persona_anchor
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
# Lelik placed the call, so he speaks first. The caller's own "hello?" usually falls into the
# few seconds before the media stream exists and is never heard, and waiting for another
# one left both sides silent for 10-20 s on every live call (2026-09-23).
_PICKUP_NOTE = "[The caller has just picked up the phone you called. Speak first.]"
_WATCHDOG_TICK_S = 0.25
_DELEGATION_TIMEOUT_S = 90.0
# Attached to every delegation by the relay, never left to the model's retention (RFC §4.7).
_CALL_CONTEXT_TURNS = 6
_LATE_ANSWER_NOTE = "[The answer to your earlier request just arrived: {output}]"
_DELEGATION_TIMED_OUT = "No answer arrived in time. Tell the caller in one line that it did not come through."
_DELEGATION_FAILED = "The request failed. Tell the caller in one line that it did not go through."
_WAITING_NOTE = "[Still waiting for the answer to your request. Keep the caller company in one short line.]"


@dataclass
class _CallState:
    """Turn state shared by the event loop and the silence watchdog of one call."""

    playback: PlaybackTracker
    response_active: bool = False
    caller_speaking: bool = False
    silence_prompted: bool = False
    item_id: Optional[str] = None
    item_start_bytes: int = 0
    # Built once per call from the session instructions; None when the prompt carries
    # no persona sections to point at.
    persona_anchor: Optional[str] = None
    caller_turns: int = 0  # speech_started count: "did the caller speak since dispatch?"
    response_owed: bool = False
    queued_answers: List[Tuple[str, str, bool]] = field(default_factory=list)
    delegations: Set[asyncio.Task] = field(default_factory=set)


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
        delegation_timeout_s: float = _DELEGATION_TIMEOUT_S,
    ) -> None:
        self._session_factory = realtime_session_factory
        self._control_plane = control_plane
        self._alert_sink = alert_sink
        self._reasoning_effort = reasoning_effort
        self._silence_timeout_s = silence_timeout_s
        self._delegation_timeout_s = delegation_timeout_s

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
        state = _CallState(playback=playback, persona_anchor=build_persona_anchor(config["instructions"]))
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
                await session.open(instructions=config["instructions"], reasoning_effort=self._reasoning_effort,
                                   tools=config.get("tools", []))
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
            await session.submit_message("system", _PICKUP_NOTE)
            await self._reply_to_turn(session, state)

            forward_task = asyncio.ensure_future(self._forward_inbound(session, inbound_audio))
            consume_task = asyncio.ensure_future(
                self._consume_events(ticket, config, session, buffer, send_outbound_audio, clear_outbound_audio, state)
            )
            # The watchdog never ends on its own; the call ends with the audio streams.
            watchdog_task = asyncio.ensure_future(self._watch_silence(session, state))
            await asyncio.wait({forward_task, consume_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            tasks = [task for task in (forward_task, consume_task, watchdog_task) if task is not None]
            # In-flight delegations die with the call: their answer served a conversation that is gone.
            tasks += list(state.delegations)
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
        config: dict,
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
                state.caller_turns += 1
                state.caller_speaking = True
                state.silence_prompted = False
                # The caller started talking over Lelik (RFC §4.7; mechanism validated in
                # scripts/voice/test_mulaw_relay_poc.py:154-170). Barge-in is ours alone -
                # the provider's auto-interrupt is off (OpenAIRealtimeAdapter._TURN_DETECTION).
                # "Talking" means audible, not generating: the provider finishes a reply
                # seconds before Twilio finishes playing it, so gating on response_active
                # alone let the whole tail play over the caller (first live call, 2026-09-22).
                if state.response_active or not state.playback.caught_up:
                    await self._barge_in(session, state, clear_outbound_audio)
            elif event.type == "speech_stopped":
                state.caller_speaking = False
            elif event.type == "turn_committed":
                await self._reply_to_turn(session, state)
            elif event.type == "audio_delta":
                item_id = event.payload.get("item_id")
                if item_id != state.item_id:
                    state.item_id = item_id
                    state.item_start_bytes = state.playback.sent_bytes
                await send_outbound_audio(event.payload["frame"])
            elif event.type == "tool_call":
                self._start_delegation(ticket, config, session, state, buffer, event.payload, pending_request_text)
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
                await self._flush_answers(session, state)
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
        # Guarded: response.cancel with nothing in flight is a provider error that would
        # end the call - a reply already fully generated is only still PLAYING.
        if state.response_active:
            await session.cancel_response()
            state.response_active = False
        if state.item_id:
            await session.truncate(state.item_id, heard_ms)
            # Truncated once; a second speech_started must not cut the same item again.
            state.item_id = None

    async def _reply_to_turn(self, session: RealtimeSessionPort, state: _CallState) -> None:
        # The provider does not reply on its own (create_response off), so every reply
        # starts here, with the persona anchor placed right after the caller's turn -
        # the realtime twin of the text path's per-turn persona anchor. Anchors are left
        # in the conversation rather than deleted: a delete of a missing item is a
        # provider error, and any provider error ends the call.
        # Guarded: response.create while a response is active is a call-ending error.
        if state.response_active:
            logger.warning("voice call: turn committed while a response is active, not starting another")
            return
        if state.persona_anchor:
            await session.submit_message("system", state.persona_anchor)
        state.response_active = True
        state.response_owed = False
        await session.request_response()

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
            note = _WAITING_NOTE if state.delegations else _SILENCE_NOTE.format(seconds=round(self._silence_timeout_s))
            await session.submit_message("system", note)
            state.response_active = True
            await session.request_response()

    async def _forward_inbound(self, session: RealtimeSessionPort, inbound_audio: AsyncIterator[AudioFrame]) -> None:
        async for frame in inbound_audio:
            await session.send_audio(frame)

    def _start_delegation(
        self, ticket: str, config: dict, session: RealtimeSessionPort, state: _CallState,
        buffer: VoiceCallBuffer, payload: dict, pending_request_text: str,
    ) -> None:
        try:
            arguments = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        call_context = buffer.recent_exchanges(_CALL_CONTEXT_TURNS)
        if pending_request_text:
            call_context.append({"role": "user", "text": pending_request_text})
        # Taken here, not when the task first runs: the event loop keeps consuming events
        # before the task starts, and a caller turn in between must count as an interruption.
        dispatched_at = state.caller_turns
        # Tracked, never fire-and-forget (RUF006): the call's teardown cancels what is left.
        task = asyncio.ensure_future(self._run_delegation(
            ticket, config, session, state, payload.get("call_id"),
            arguments if isinstance(arguments, dict) else {}, call_context, dispatched_at,
        ))
        state.delegations.add(task)
        task.add_done_callback(state.delegations.discard)

    async def _run_delegation(
        self, ticket: str, config: dict, session: RealtimeSessionPort, state: _CallState,
        call_id: str, arguments: dict, call_context: list, dispatched_at: int,
    ) -> None:
        try:
            output = await asyncio.wait_for(
                self._control_plane.delegate(
                    user_id=config["user_id"], account_id=config["account_id"],
                    arguments=arguments, call_context=call_context,
                ),
                timeout=self._delegation_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(f"voice call {ticket}: delegation {call_id} timed out")
            output = _DELEGATION_TIMED_OUT
        except asyncio.CancelledError:
            logger.info(f"voice call {ticket}: call ended, delegation {call_id} discarded")
            raise
        except Exception as exc:
            # No retry: it re-runs the whole pipeline (double spend, double chat copy).
            logger.error(f"voice call {ticket}: delegation {call_id} failed: {exc}")
            output = _DELEGATION_FAILED
        await self._resolve_late_answer(session, state, call_id, output, state.caller_turns > dispatched_at)

    async def _resolve_late_answer(
        self, session: RealtimeSessionPort, state: _CallState, call_id: str, output: str, interrupted: bool,
    ) -> None:
        # The one seam RFC §4.7 names; swap the policy here, nowhere else.
        if state.response_active:
            # response.create during an active response is a call-ending provider error.
            state.queued_answers.append((call_id, output, interrupted))
            return
        await self._inject_answer(session, call_id, output, interrupted)
        await self._reply_or_owe(session, state)

    async def _inject_answer(self, session: RealtimeSessionPort, call_id: str, output: str, interrupted: bool) -> None:
        # Spike 0.1: after the caller spoke, OpenAI drops a late function_call_output silently.
        if interrupted:
            await session.submit_message("system", _LATE_ANSWER_NOTE.format(output=output))
        else:
            await session.submit_tool_result(call_id, output)

    async def _reply_or_owe(self, session: RealtimeSessionPort, state: _CallState) -> None:
        # A caller mid-sentence keeps the floor; their committed turn starts the reply.
        if state.caller_speaking:
            state.response_owed = True
            return
        await self._reply_to_turn(session, state)

    async def _flush_answers(self, session: RealtimeSessionPort, state: _CallState) -> None:
        if not state.queued_answers:
            return
        queued, state.queued_answers = state.queued_answers, []
        for call_id, output, interrupted in queued:
            await self._inject_answer(session, call_id, output, interrupted)
        # All answers in, then one reply: two response.create can never collide.
        await self._reply_or_owe(session, state)
