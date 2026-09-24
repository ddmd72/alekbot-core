import asyncio
import base64
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, List, NamedTuple, Optional, Set

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
# Thinking cue: 20 ms μ-law frames, paced in real time with a short lead, so stopping it
# never leaves more than ~100 ms queued in front of Lelik's first word.
_CUE_FRAME_BYTES = 160
_CUE_LEAD_BYTES = 800
_CUE_TICK_S = 0.02
_MULAW_BYTES_PER_S = 8000
# Replies to the caller or to an arriving answer; the greeting and the watchdog's own
# notes are Lelik's initiative, so a "thinking" sound before them would be noise.
_CUE_REASONS = frozenset({"turn", "answer"})
_DELEGATION_TIMEOUT_S = 90.0
# Attached to every delegation by the relay, never left to the model's retention (RFC §4.7).
_CALL_CONTEXT_TURNS = 6
_LATE_ANSWER_NOTE = "[The answer to your earlier request ({request}) just arrived: {output}]"
# Distinct from _LATE_ANSWER_NOTE: formatting the timeout/failure sentinels into it read as
# self-contradicting ("just arrived: No answer arrived in time...").
_LATE_TIMEOUT_NOTE = ("[Your earlier request ({request}) got no answer in time. Tell the caller in "
                       "one line that it did not come through.]")
_LATE_FAILURE_NOTE = ("[Your earlier request ({request}) failed. Tell the caller in one line that it "
                      "did not go through.]")
_REQUEST_LABEL_CHARS = 120
_DELEGATION_TIMED_OUT = "No answer arrived in time. Tell the caller in one line that it did not come through."
_DELEGATION_FAILED = "The request failed. Tell the caller in one line that it did not go through."
_WAITING_NOTE = ("[Still waiting for the answer to your request. Keep the caller company: pick up a thread "
                 "from this conversation and riff on it with your humor, a few sentences. "
                 "Do not talk about the waiting itself.]")
# A barge-in cancels the response, but its function_call_arguments.done can still arrive a few ms
# later (live, 2026-09-24 09:29:09) — the function_call item still needs an output or it is left
# dangling for the next turn, but running it would spend ~30s of Smart on half a question.
_CANCELLED_TOOL_CALL = "Not run: the caller interrupted before this request was complete."


def _request_label(arguments: dict) -> str:
    return f"{arguments.get('intent', '')}: {arguments.get('query', '')}"[:_REQUEST_LABEL_CHARS]


class _Answer(NamedTuple):
    call_id: str
    output: str
    dispatched_at: int  # caller_turns at dispatch: "interrupted" is judged when injected, not when queued.
    request: str  # A system note has no call_id, so it names the request it answers.


@dataclass
class _CallState:
    """Turn state shared by the event loop and the silence watchdog of one call."""

    playback: PlaybackTracker
    ticket: str
    response_active: bool = False
    # The active response was barged into (explains an empty transcript; its tool calls are not run).
    response_cancelled: bool = False
    # Speech over Lelik waiting to prove it is an interruption, not a blip (barge_in_min_speech_s).
    pending_barge_in: Optional[asyncio.Task] = None
    # The caller's last speech was too short to interrupt Lelik: its committed turn gets no reply.
    speech_dismissed: bool = False
    response_reason: str = ""
    response_audible: bool = False  # the active response has sent its first audio
    response_started_at: float = 0.0
    # loop.time() until which cue audio may still be queued at Twilio: cleared before speech.
    cue_tail_until: float = 0.0
    caller_speaking: bool = False
    silence_prompted: bool = False
    item_id: Optional[str] = None
    item_start_bytes: int = 0
    # Built once per call from the session instructions; None when the prompt carries
    # no persona sections to point at.
    persona_anchor: Optional[str] = None
    caller_turns: int = 0  # speech_started count: "did the caller speak since dispatch?"
    response_owed: bool = False
    # Answers being injected and not yet replied to: the watchdog holds off, or its note takes the slot.
    answers_in_flight: int = 0
    queued_answers: List[_Answer] = field(default_factory=list)
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
        thinking_cue: bytes = b"",
        cue_grace_s: float = 0.7,
        barge_in_min_speech_s: float = 0.0,
        hangup_after_silence_s: Optional[float] = None,
    ) -> None:
        self._session_factory = realtime_session_factory
        self._control_plane = control_plane
        self._alert_sink = alert_sink
        self._reasoning_effort = reasoning_effort
        self._silence_timeout_s = silence_timeout_s
        self._delegation_timeout_s = delegation_timeout_s
        self._thinking_cue = thinking_cue
        self._cue_grace_s = cue_grace_s
        self._barge_in_min_speech_s = barge_in_min_speech_s
        self._hangup_after_silence_s = hangup_after_silence_s

    async def handle_call(
        self,
        ticket: str,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        playback: PlaybackTracker,
        send_cue_audio: Optional[Callable[[AudioFrame], Awaitable[None]]] = None,
    ) -> None:
        """`playback` is fed by the transport (bytes sent, marks echoed back); this
        loop only reads it. `send_cue_audio` plays filler that playback never counts."""
        config = await self._control_plane.fetch_session_config(ticket)
        state = _CallState(playback=playback, ticket=ticket, persona_anchor=build_persona_anchor(config["instructions"]))
        async with RequestContext(user_id=config["user_id"], account_id=config["account_id"]):
            await self._run_call(ticket, config, inbound_audio, send_outbound_audio, clear_outbound_audio, state,
                                 send_cue_audio)

    async def _run_call(
        self,
        ticket: str,
        config: dict,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        state: _CallState,
        send_cue_audio: Optional[Callable[[AudioFrame], Awaitable[None]]] = None,
    ) -> None:
        session = self._session_factory()
        buffer = VoiceCallBuffer(call_id=ticket)
        forward_task: Optional[asyncio.Task] = None
        consume_task: Optional[asyncio.Task] = None
        watchdog_task: Optional[asyncio.Task] = None
        cue_task: Optional[asyncio.Task] = None

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
            await self._reply_to_turn(session, state, "pickup")

            forward_task = asyncio.ensure_future(self._forward_inbound(session, inbound_audio))
            consume_task = asyncio.ensure_future(
                self._consume_events(ticket, config, session, buffer, send_outbound_audio, clear_outbound_audio, state)
            )
            # The watchdog never ends on its own; the call ends with the audio streams.
            watchdog_task = asyncio.ensure_future(self._watch_silence(session, state))
            if self._thinking_cue and send_cue_audio is not None:
                cue_task = asyncio.ensure_future(self._play_cue(state, send_cue_audio))
            # The watchdog ends only to hang up after a long silence (hangup_after_silence_s).
            await asyncio.wait({forward_task, consume_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            tasks = [task for task in (forward_task, consume_task, watchdog_task, cue_task) if task is not None]
            # In-flight delegations die with the call: their answer served a conversation that is gone.
            tasks += list(state.delegations)
            if state.pending_barge_in is not None:
                tasks.append(state.pending_barge_in)
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
        tool_called = False  # Per response: this or a barge-in explains an empty transcript.
        async for event in session.receive_events():
            if event.type == "response_created":
                turn_start = datetime.now(timezone.utc)
                state.response_active = True
                state.response_cancelled = False
                tool_called = False
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
                state.speech_dismissed = False
                if state.response_active or not state.playback.caught_up:
                    if self._barge_in_min_speech_s > 0:
                        # A line blip or an "uh-huh" also starts speech; only sustained speech
                        # interrupts (live, 2026-09-25: replies cut by 400 ms noises).
                        if state.pending_barge_in is None:
                            state.pending_barge_in = asyncio.ensure_future(
                                self._confirm_barge_in(session, state, clear_outbound_audio))
                    else:
                        await self._barge_in(session, state, clear_outbound_audio)
                elif self._cue_tail_queued(state):
                    await clear_outbound_audio()
                state.cue_tail_until = 0.0
            elif event.type == "speech_stopped":
                state.caller_speaking = False
                if state.pending_barge_in is not None and not state.pending_barge_in.done():
                    state.pending_barge_in.cancel()
                    state.pending_barge_in = None
                    state.speech_dismissed = True
                    logger.info(f"voice call {ticket}: speech too short to interrupt, Lelik keeps talking")
            elif event.type == "turn_committed":
                if state.speech_dismissed:
                    # Lelik is still mid-reply; the words stay in the conversation for his next turn.
                    state.speech_dismissed = False
                    logger.info(f"voice call {ticket}: short turn over Lelik not answered")
                else:
                    await self._reply_to_turn(session, state, "turn")
            elif event.type == "audio_delta":
                if not state.response_audible:
                    state.response_audible = True
                    elapsed_ms = round((asyncio.get_running_loop().time() - state.response_started_at) * 1000)
                    logger.info(f"voice call {ticket}: first audio after {elapsed_ms} ms ({state.response_reason})")
                    if self._cue_tail_queued(state):
                        # Only cue audio can be queued here (the cue plays only once playback caught up).
                        await clear_outbound_audio()
                    state.cue_tail_until = 0.0
                item_id = event.payload.get("item_id")
                if item_id != state.item_id:
                    state.item_id = item_id
                    state.item_start_bytes = state.playback.sent_bytes
                await send_outbound_audio(event.payload["frame"])
            elif event.type == "tool_call":
                tool_called = True
                if state.response_cancelled:
                    call_id = event.payload.get("call_id")
                    logger.info(f"voice call {ticket}: delegation {call_id} skipped (response cancelled)")
                    await session.submit_tool_result(call_id, _CANCELLED_TOOL_CALL)
                else:
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
                if not (pending_response_text or tool_called or state.response_cancelled):
                    logger.info(f"voice call {ticket}: response done with an empty transcript")
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
    ) -> bool:
        """Returns whether an in-flight response was cancelled."""
        # Order matters. Read what was heard BEFORE clearing: Twilio echoes the marks of
        # dropped audio after a clear, which would count unheard audio as played. Then
        # drain both buffers - Twilio's queued playback and the provider's generation -
        # and cut the item to the heard part, so the model resumes from where it was
        # actually interrupted instead of believing it finished the reply.
        heard_ms = state.playback.played_ms_since(state.item_start_bytes)
        cancelled = state.response_active
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
        logger.info(f"voice call {state.ticket}: barge-in, heard {heard_ms} ms, cancelled={cancelled}")
        state.response_cancelled = state.response_cancelled or cancelled
        return cancelled

    async def _confirm_barge_in(
        self, session: RealtimeSessionPort, state: _CallState,
        clear_outbound_audio: Callable[[], Awaitable[None]],
    ) -> None:
        await asyncio.sleep(self._barge_in_min_speech_s)
        state.pending_barge_in = None
        # Re-checked: Lelik may have finished (or the caller stopped) while we waited.
        if state.caller_speaking and (state.response_active or not state.playback.caught_up):
            try:
                await self._barge_in(session, state, clear_outbound_audio)
            except Exception as exc:
                logger.error(f"voice call {state.ticket}: barge-in failed: {exc}", exc_info=True)
                raise

    async def _reply_to_turn(self, session: RealtimeSessionPort, state: _CallState, reason: str) -> None:
        # The provider does not reply on its own (create_response off), so every reply
        # starts here, with the persona anchor placed right after the caller's turn -
        # the realtime twin of the text path's per-turn persona anchor. Anchors are left
        # in the conversation rather than deleted: a delete of a missing item is a
        # provider error, and any provider error ends the call.
        await self._start_response(session, state, state.persona_anchor, reason)

    async def _start_response(
        self, session: RealtimeSessionPort, state: _CallState, note: Optional[str], reason: str,
    ) -> None:
        # The only place a response starts. response.create while a response is active is a
        # call-ending error, so the slot is claimed before the first await: a concurrent
        # starter (watchdog, a second answer) sees it taken instead of racing past the guard.
        if state.response_active:
            logger.warning(f"voice call {state.ticket}: a response is already active, not starting another ({reason})")
            return
        state.response_active = True
        state.response_owed = False
        state.response_reason = reason
        state.response_audible = False
        state.response_started_at = asyncio.get_running_loop().time()
        logger.info(f"voice call {state.ticket}: starting response ({reason})")
        if note:
            await session.submit_message("system", note)
        await session.request_response()

    async def _watch_silence(self, session: RealtimeSessionPort, state: _CallState) -> None:
        # The provider only speaks when a turn ends, so it cannot notice a caller who
        # has gone quiet (and its idle_timeout_ms is server_vad-only). Silence counts
        # from the moment Lelik's audio has finished PLAYING, not finished generating.
        # One prompt per silence, re-armed by the caller speaking; while a delegation is
        # pending every note re-arms it, so Lelik keeps the caller company until the answer.
        loop = asyncio.get_running_loop()
        quiet_since = loop.time()
        while True:
            await asyncio.sleep(_WATCHDOG_TICK_S)
            now = loop.time()
            busy = state.response_active or state.answers_in_flight or state.caller_speaking
            if busy or not state.playback.caught_up:
                quiet_since = now
                continue
            if (self._hangup_after_silence_s is not None and state.silence_prompted and not state.delegations
                    and now - quiet_since >= self._hangup_after_silence_s):
                # The one "still there?" went unanswered: a voicemail box or a phone put down.
                logger.info(f"voice call {state.ticket}: hanging up after {round(now - quiet_since)} s of silence")
                return
            if (state.silence_prompted and not state.delegations) or now - quiet_since < self._silence_timeout_s:
                continue
            state.silence_prompted = True
            quiet_since = now
            if state.delegations:
                await self._start_response(session, state, _WAITING_NOTE, "waiting")
            else:
                note = _SILENCE_NOTE.format(seconds=round(self._silence_timeout_s))
                await self._start_response(session, state, note, "silence")

    @staticmethod
    def _cue_tail_queued(state: _CallState) -> bool:
        return asyncio.get_running_loop().time() < state.cue_tail_until

    @staticmethod
    def _cue_wanted(state: _CallState) -> bool:
        """Something is owed and nothing is audible: Lelik is thinking, or a delegation is out."""
        if state.caller_speaking or not state.playback.caught_up:
            return False
        speaking = state.response_active and state.response_audible
        thinking = state.response_active and not state.response_audible and state.response_reason in _CUE_REASONS
        waiting = bool(state.delegations or state.answers_in_flight) and not speaking
        return thinking or waiting

    async def _play_cue(self, state: _CallState, send_cue_audio: Callable[[AudioFrame], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()
        clip = self._thinking_cue
        # Long enough that any 20 ms window starting inside the clip is one contiguous slice.
        looped = clip * (_CUE_FRAME_BYTES // len(clip) + 2)
        wanted_since: Optional[float] = None
        started_at: Optional[float] = None
        sent = position = 0
        while True:
            await asyncio.sleep(_CUE_TICK_S)
            now = loop.time()
            if not self._cue_wanted(state):
                if started_at is not None:
                    logger.info(f"voice call {state.ticket}: cue stopped after {round((now - started_at) * 1000)} ms")
                wanted_since = started_at = None
                continue
            if wanted_since is None:
                wanted_since = now
            if now - wanted_since < self._cue_grace_s:
                continue
            if started_at is None:
                # Each pause starts the clip from its first sound, not mid-silence.
                started_at, sent, position = now, 0, 0
                kind = "thinking" if state.response_active else "waiting"
                logger.info(f"voice call {state.ticket}: cue started ({kind})")
            while sent < (now - started_at) * _MULAW_BYTES_PER_S + _CUE_LEAD_BYTES and self._cue_wanted(state):
                chunk = looped[position:position + _CUE_FRAME_BYTES]
                await send_cue_audio(AudioFrame(
                    encoding="audio/pcmu", sample_rate_hz=8000,
                    payload=base64.b64encode(chunk).decode(), track="outbound",
                ))
                sent += _CUE_FRAME_BYTES
                position = (position + _CUE_FRAME_BYTES) % len(clip)
                state.cue_tail_until = started_at + sent / _MULAW_BYTES_PER_S

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
        if not isinstance(arguments, dict):
            arguments = {}
        logger.info(f"voice call {ticket}: delegation {payload.get('call_id')} dispatched ({arguments.get('intent')})")
        # Tracked, never fire-and-forget (RUF006): the call's teardown cancels what is left.
        task = asyncio.ensure_future(self._run_delegation(
            ticket, config, session, state, payload.get("call_id"),
            arguments, call_context, dispatched_at,
        ))
        state.delegations.add(task)
        task.add_done_callback(state.delegations.discard)

    async def _run_delegation(
        self, ticket: str, config: dict, session: RealtimeSessionPort, state: _CallState,
        call_id: str, arguments: dict, call_context: list, dispatched_at: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            output = await self._fetch_answer(ticket, config, call_id, arguments, call_context)
            elapsed_ms = round((loop.time() - started) * 1000)
            answer = _Answer(call_id, output, dispatched_at, _request_label(arguments))
            await self._resolve_late_answer(session, state, answer, elapsed_ms)
        except asyncio.CancelledError:
            logger.info(f"voice call {ticket}: call ended, delegation {call_id} discarded")
            raise
        except Exception as exc:
            # Nothing gathers a finished delegation task, so an error here is logged or lost.
            logger.error(f"voice call {ticket}: answer to delegation {call_id} not delivered: {exc}", exc_info=True)

    async def _fetch_answer(self, ticket: str, config: dict, call_id: str, arguments: dict, call_context: list) -> str:
        try:
            return await asyncio.wait_for(
                self._control_plane.delegate(
                    user_id=config["user_id"], account_id=config["account_id"],
                    arguments=arguments, call_context=call_context,
                ),
                timeout=self._delegation_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(f"voice call {ticket}: delegation {call_id} timed out")
            return _DELEGATION_TIMED_OUT
        except Exception as exc:
            # No retry: it re-runs the whole pipeline (double spend, double chat copy).
            logger.error(f"voice call {ticket}: delegation {call_id} failed: {exc}")
            return _DELEGATION_FAILED

    async def _resolve_late_answer(
        self, session: RealtimeSessionPort, state: _CallState, answer: _Answer, elapsed_ms: int,
    ) -> None:
        # The one seam RFC §4.7 names; swap the policy here, nowhere else.
        if state.response_active:
            # response.create during an active response is a call-ending provider error.
            logger.info(f"voice call {state.ticket}: answer {answer.call_id} arrived after {elapsed_ms} ms, queued")
            state.queued_answers.append(answer)
            return
        logger.info(f"voice call {state.ticket}: answer {answer.call_id} arrived after {elapsed_ms} ms, injecting")
        await self._deliver_answers(session, state, [answer])

    async def _deliver_answers(self, session: RealtimeSessionPort, state: _CallState, answers: List[_Answer]) -> None:
        # Marked before the first await: the watchdog re-arms while a delegation is pending, and
        # a waiting note slipped in mid-injection would take the reply slot and bury the answer.
        state.answers_in_flight += 1
        try:
            for answer in answers:
                await self._inject_answer(session, state, answer)
            # All answers in, then one reply: two response.create can never collide.
            await self._reply_or_owe(session, state)
        finally:
            state.answers_in_flight -= 1

    async def _inject_answer(self, session: RealtimeSessionPort, state: _CallState, answer: _Answer) -> None:
        # Spike 0.1: after the caller spoke, OpenAI drops a late function_call_output silently.
        if state.caller_turns > answer.dispatched_at:
            if answer.output == _DELEGATION_TIMED_OUT:
                note = _LATE_TIMEOUT_NOTE.format(request=answer.request)
            elif answer.output == _DELEGATION_FAILED:
                note = _LATE_FAILURE_NOTE.format(request=answer.request)
            else:
                note = _LATE_ANSWER_NOTE.format(request=answer.request, output=answer.output)
            await session.submit_message("system", note)
            channel = "system note"
        else:
            await session.submit_tool_result(answer.call_id, answer.output)
            channel = "function_call_output"
        logger.info(f"voice call {state.ticket}: answer {answer.call_id} injected as {channel}, "
                    f"{len(answer.output)} chars")

    async def _reply_or_owe(self, session: RealtimeSessionPort, state: _CallState) -> None:
        # A caller mid-sentence keeps the floor; their committed turn starts the reply.
        if state.caller_speaking:
            state.response_owed = True
            return
        await self._reply_to_turn(session, state, "answer")

    async def _flush_answers(self, session: RealtimeSessionPort, state: _CallState) -> None:
        if not state.queued_answers:
            return
        queued, state.queued_answers = state.queued_answers, []
        logger.info(f"voice call {state.ticket}: flushing {len(queued)} queued answers")
        await self._deliver_answers(session, state, queued)
