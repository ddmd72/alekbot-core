import asyncio
import base64
import json
import random
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Dict, List, NamedTuple, Optional, Sequence, Set

from src.domain.llm import build_persona_anchor
from src.domain.log_redaction import redact_exception, redact_for_log
from src.domain.remote_tool_server import RemoteToolServer
from src.domain.request_context import RequestContext
from src.domain.voice_ask_latch import AskLatch, AskReason, AskState, AskStep
from src.domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD, VOICE_MCP_TOKENS_FIELD, pair_session_servers
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_control_protocol import ASK_ARM, ASK_DISARM, FEATURE_ASK, ask_state_frame, command_seq
from src.domain.voice_errand import delegation_arguments, is_errand
from src.domain.voice_mode import VoiceMode
from src.domain.voice_call_buffer import VoiceCallBuffer, VoiceTurnSegment
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import LEGACY_VOICE_SESSION
from src.domain.voice_resume import RESUME_CONFIG_FIELD
from src.domain.voice_session_spec import VoiceSessionSpec
from src.domain.voice_turn_ownership import TurnOwnership
from src.ports.alert_sink import AlertSinkPort
from src.ports.call_control_plane_port import CallControlPlanePort
from src.ports.realtime_session_port import RealtimeSessionPort
from src.domain.secret_digest import fingerprint
from src.utils.logger import logger

if TYPE_CHECKING:  # type-only: services/ do not import each other at runtime (REQ-ARCH-22)
    from src.services.call_control_hub import CallControlHub, CallControlLink

_UNKNOWN_MODEL_LABEL = "unknown"
# SPOKEN_DELIVERY's `silence` rule reacts to this exact kind of note.
_SILENCE_NOTE = "[The caller has been silent for {seconds} seconds.]"
# Lelik placed the call, so he speaks first. The caller's own "hello?" usually falls into the
# few seconds before the media stream exists and is never heard, and waiting for another
# one left both sides silent for 10-20 s on every live call (2026-09-23).
_PICKUP_NOTE = "[The caller has just picked up the phone you called. Speak first.]"
# The web page (VOICE_WEB_TRANSPORT_RFC §5.3 #6): the caller pressed "call"; Lelik still opens.
_WEB_PICKUP_NOTE = "[The caller has just connected to you from the web page. Speak first.]"
# "Ask Lelik" (VOICE_CALL_CONTROL_CHANNEL_RFC §5.3, owner Q2): the caller pressed the button on the interpreter page,
# and this note goes in when their next words start, so it lands right before the turn it marks. A system-role item
# like the pickup note; it never enters a user message, and it is not deleted afterwards (a delete of a missing item
# is a call-ending provider error).
_ASK_NOTE = ("[The caller's next words are addressed to you, not for translation: a question or a remark for you, such "
             "as where they are or which language to use. Answer the caller in their language; do not translate "
             "these words.]")
# The control task's beat: page commands are handled at once; timeouts and the consume loop's state frames go out on it.
_ASK_TICK_S = 0.25
# A resumed call (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2): describes the situation and gives no lines to say, so Lelik
# opens in his own words and shows he is still in the topic.
_RESUME_NOTE = ("[The call dropped and the caller has just dialled back. The conversation above is the same one, "
                "continuing. Speak first, in your own words, and show you know where it stopped.]")
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
_CALL_CONTEXT_TURNS = 12  # the caller's own words are Alek's source of truth (RFC §4.15.4)
_LATE_ANSWER_NOTE = "[The answer to your earlier request ({request}) just arrived: {output}]"
# Distinct from _LATE_ANSWER_NOTE: formatting the timeout/failure sentinels into it read as
# self-contradicting ("just arrived: No answer arrived in time...").
# The relay stopped waiting, not the main service: the answer still comes, to the user's chat
# (the main side posts it once the relay abandons the delegation, owner 2026-09-28).
_LATE_TIMEOUT_NOTE = ("[Your earlier request ({request}) is taking long. Tell the caller in one line "
                      "that the answer will come to their chat.]")
_LATE_FAILURE_NOTE = ("[Your earlier request ({request}) failed. Tell the caller in one line that it "
                      "did not go through.]")
_REQUEST_LABEL_CHARS = 120
_DELEGATION_TIMED_OUT = "The answer is taking long. Tell the caller in one line that it will come to their chat."
# Abandoning is best effort and runs during call teardown: bounded, so it never holds the call up.
_ABANDON_TIMEOUT_S = 5.0
_DELEGATION_FAILED = "The request failed. Tell the caller in one line that it did not go through."
# Signals only: how to fill the pause lives once, in COGNITIVE_PROCESS_LELIK. Notes that each
# said "riff on it with your humor, a few sentences" produced a monologue per note, and "from
# this conversation" left nothing to draw on early in a call (owner, 2026-09-28).
_WAITING_NOTE = "[Still waiting for the answer to your request.]"
# A barge-in cancels the response, but its function_call_arguments.done can still arrive a few ms
# later (live, 2026-09-24 09:29:09) — the function_call item still needs an output or it is left
# dangling for the next turn, but running it would spend ~30s of Smart on half a question.
_CANCELLED_TOOL_CALL = "Not run: the caller interrupted before this request was complete."


def _config_secrets(config: object) -> tuple:
    """The call's tokens still on a raw session config, for redaction only; () when there are none."""
    tokens = config.get(VOICE_MCP_TOKENS_FIELD) if isinstance(config, dict) else None
    return tuple(t for t in tokens.values() if isinstance(t, str)) if isinstance(tokens, dict) else ()


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
    greeting_audible_at: Optional[float] = None
    speech_in_greeting_guard: bool = False
    # loop.time() until which cue audio may still be queued at Twilio: cleared before speech.
    cue_tail_until: float = 0.0
    caller_speaking: bool = False
    silence_prompted: bool = False
    item_id: Optional[str] = None
    item_start_bytes: int = 0
    # Built once per call from the session instructions; None when the prompt carries
    # no persona sections to point at.
    persona_anchor: Optional[str] = None
    # Per call (VOICE_MULTI_PROVIDER_RFC §4.3): one relay serves users on different providers.
    turn_ownership: TurnOwnership = TurnOwnership.RELAY
    caller_turns: int = 0  # speech_started count: "did the caller speak since dispatch?"
    response_owed: bool = False
    # Answers being injected and not yet replied to: the watchdog holds off, or its note takes the slot.
    answers_in_flight: int = 0
    queued_answers: List[_Answer] = field(default_factory=list)
    delegations: Set[asyncio.Task] = field(default_factory=set)
    # Delegation task -> realtime call_id, while the relay still waits for its answer. What is left
    # here when the call ends is abandoned, so the main service posts those answers to chat.
    pending_delegations: Dict[asyncio.Task, str] = field(default_factory=dict)
    # send_to_chat errands (RFC §4.15.2): tracked for teardown only. They are not "delegations" —
    # nothing waits for them, so no dispatch filler, no waiting notes, no abandon.
    errands: Set[asyncio.Task] = field(default_factory=set)
    # "Ask Lelik" (interpreter calls with a control link only): the latch, its link, and the `ask_state` frames the
    # consume loop produced for the control task to send (the consume loop never waits on the page's socket).
    ask: Optional[AskLatch] = None
    ask_link: Optional["CallControlLink"] = None
    ask_outbox: List[dict] = field(default_factory=list)
    # The provider's conversation id (xAI), reported with the transcript so a re-dial can resume.
    resume_handle: Optional[str] = None


class VoiceSessionService:
    """The relay's session loop (RFC §4.5). Not an agent - REQ-ARCH-03/-30
    forbid naming this *Agent (RFC §4.4)."""

    def __init__(
        self,
        realtime_session_factory: Callable[[VoiceSessionSpec], RealtimeSessionPort],
        control_plane: CallControlPlanePort,
        alert_sink: AlertSinkPort,
        reasoning_effort: Optional[str] = None,
        silence_timeout_s: float = 8.0,
        delegation_timeout_s: float = _DELEGATION_TIMEOUT_S,
        thinking_cue: bytes = b"",
        cue_grace_s: float = 0.7,
        barge_in_min_speech_s: float = 0.0,
        hangup_after_silence_s: Optional[float] = None,
        caller_opening: Optional[str] = None,
        waiting_gap_max_s: Optional[float] = None,
        greeting_guard_s: float = 0.0,
        control_hub: Optional["CallControlHub"] = None,
        ask_clock: Optional[Callable[[], float]] = None,
        ask_tick_s: float = _ASK_TICK_S,
    ) -> None:
        """The provider, its turn ownership, voice and effort come per call from the session
        config's "voice" spec (VOICE_MULTI_PROVIDER_RFC §4.2). `reasoning_effort`, when given,
        overrides the spec's.

        Under PROVIDER turn ownership the provider replies to the caller and stops when talked
        over. The relay then places no persona anchor, sends no caller opening and no silence or
        waiting notes, and on barge-in only drops the queued audio. It still starts the greeting
        and delivers delegation answers.

        `control_hub` (web calls only, VOICE_CALL_CONTROL_CHANNEL_RFC §4.3): a call whose session config carries
        `control_token_sha256` opens its control link there for the call's life. An interpreter call's link offers
        "Ask Lelik" (RFC §5): `ask_clock` is the latch's clock (the event loop's time when None; tests pass their
        own), `ask_tick_s` the control task's beat."""
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
        self._caller_opening = caller_opening
        self._waiting_gap_max_s = waiting_gap_max_s
        self._greeting_guard_s = greeting_guard_s
        self._control_hub = control_hub
        self._ask_clock = ask_clock
        self._ask_tick_s = ask_tick_s

    def _in_greeting_guard(self, state: "_CallState") -> bool:
        # The browser's echo canceller adapts over the first second or two of far-end audio, and
        # the greeting is the call's first sound: live web calls heard Lelik's own "Радий" back as
        # the caller ("Radi."), cut the greeting and greeted twice (2026-09-28). Speech that starts
        # this early in the greeting never interrupts it.
        if self._greeting_guard_s <= 0 or state.response_reason != "pickup" or state.greeting_audible_at is None:
            return False
        return asyncio.get_running_loop().time() - state.greeting_audible_at < self._greeting_guard_s

    def _next_waiting_gap(self) -> float:
        # While a delegation is out, notes at a fixed beat read as a metronome (owner, 2026-09-28):
        # each gap is drawn from [silence_timeout_s, waiting_gap_max_s].
        if self._waiting_gap_max_s is None or self._waiting_gap_max_s <= self._silence_timeout_s:
            return self._silence_timeout_s
        return random.uniform(self._silence_timeout_s, self._waiting_gap_max_s)

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
        config = fetched = await self._control_plane.fetch_session_config(ticket)
        # From here on the call has tokens (minted at the fetch): every path ends in submit_transcript, which also
        # revokes them (LELIK_VOICE_MCP_RFC §5.3). Until _run_call takes over, a failure is reported here.
        try:
            # The call's remote MCP servers with their tokens (§4): paired and bound here, then the tokens leave the
            # config, so the only holders for the call's life are the session object and its redaction.
            mcp_servers, dropped = pair_session_servers(config)
            config = {key: value for key, value in config.items() if key != VOICE_MCP_TOKENS_FIELD}
            for label in dropped:
                logger.warning(f"voice call {fingerprint(ticket)}: remote tool server {label} dropped")
            spec = VoiceSessionSpec.from_dict(config["voice"]) if config.get("voice") else LEGACY_VOICE_SESSION
            relay_owned = spec.turn_ownership is TurnOwnership.RELAY
            state = _CallState(playback=playback, ticket=ticket, turn_ownership=spec.turn_ownership,
                               persona_anchor=build_persona_anchor(config["instructions"]) if relay_owned else None)
            context = RequestContext(user_id=config["user_id"], account_id=config["account_id"])
        except Exception as exc:
            logger.error(f"voice call {fingerprint(ticket)}: session config unusable: "
                         f"{redact_exception(exc, secrets=_config_secrets(fetched))}")
            await self._report_unopened_call(ticket, fetched)
            raise
        # The fetched config still holds the tokens: not kept for the call's life.
        del fetched
        async with context:
            await self._run_call(ticket, config, spec, inbound_audio, send_outbound_audio, clear_outbound_audio,
                                 state, send_cue_audio, mcp_servers=mcp_servers)

    async def _report_unopened_call(self, ticket: str, config: dict) -> None:
        """The call end for a call that failed before its session existed: the main side releases the one-call marker
        and revokes the call's tokens. Its own failure is logged and swallowed, so the original error propagates."""
        try:
            await self._control_plane.submit_transcript(
                call_id=ticket,
                user_id=str(config.get("user_id") or ""),
                account_id=str(config.get("account_id") or ""),
                buffer=VoiceCallBuffer(call_id=ticket),
            )
        except Exception as exc:
            logger.error(f"voice call {fingerprint(ticket)}: reporting the failed call end failed: "
                         f"{type(exc).__name__}")

    async def _run_call(
        self,
        ticket: str,
        config: dict,
        spec: VoiceSessionSpec,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        state: _CallState,
        send_cue_audio: Optional[Callable[[AudioFrame], Awaitable[None]]] = None,
        *,
        mcp_servers: Sequence[RemoteToolServer] = (),
    ) -> None:
        buffer = VoiceCallBuffer(call_id=ticket)
        session: Optional[RealtimeSessionPort] = None
        forward_task: Optional[asyncio.Task] = None
        consume_task: Optional[asyncio.Task] = None
        watchdog_task: Optional[asyncio.Task] = None
        cue_task: Optional[asyncio.Task] = None
        control_task: Optional[asyncio.Task] = None
        control_link: Optional["CallControlLink"] = None
        # The call's tokens, only to scrub them by value from provider and exception text (§5.4).
        secrets = tuple(server.token for server in mcp_servers if server.token)

        try:
            # Inside the try: a factory that raises must still reach submit_transcript (the revoke) below.
            session = self._session_factory(spec)
            # First, so the page's control socket can attach while the provider session opens. Only an interpreter
            # call offers "Ask Lelik" (RFC §4.2 "Features per call"); its state is set before any socket can attach,
            # so the first `welcome` already carries `ask: "idle"`.
            if self._control_hub is not None and config.get("control_token_sha256"):
                features = {FEATURE_ASK} if config.get("mode") == VoiceMode.INTERPRETER.value else set()
                control_link = self._control_hub.open(config["control_token_sha256"], fingerprint(ticket), features)
                if FEATURE_ASK in features:
                    state.ask = AskLatch(self._ask_clock or asyncio.get_running_loop().time)
                    state.ask_link = control_link
                    control_link.set_state(ask=AskState.IDLE.wire)
                    control_task = asyncio.ensure_future(self._run_ask_control(control_link, state))
            # open() is inside the try/finally, not before it: a failed open (e.g. a
            # transient connection error to the provider) must still reach close()
            # and submit_transcript() below - otherwise the relay's one-call-per-user
            # marker (RFC §3) only releases via TTL instead of immediately, and the
            # failure leaves no record on the main-service side.
            # Labels only: a server's URL and token are never logged (LELIK_VOICE_MCP_RFC §5.4).
            if mcp_servers:
                logger.info(f"voice call {fingerprint(ticket)}: remote tool servers offered: "
                            f"{', '.join(server.label for server in mcp_servers)}")
            elif config.get(VOICE_MCP_SERVERS_FIELD):
                logger.info(f"voice call {fingerprint(ticket)}: remote tool servers not offered (no call tokens)")
            resume_handle = (config.get(RESUME_CONFIG_FIELD) or {}).get("handle")
            try:
                # The handle and the servers go only when there are some: a provider without resumption or MCP never
                # sees the argument. A resumed session gets its servers too (its session.update follows the replay).
                await session.open(instructions=config["instructions"],
                                   reasoning_effort=self._reasoning_effort or spec.reasoning_effort,
                                   tools=config.get("tools", []),
                                   **({"resume_handle": resume_handle} if resume_handle else {}),
                                   **({"mcp_servers": mcp_servers} if mcp_servers else {}))
            except Exception as exc:
                # Type and a redacted message: an exception text may carry a URL, a header or a token (§5.4).
                logger.error(f"voice call {fingerprint(ticket)}: failed to open realtime session: "
                             f"{redact_exception(exc, secrets=secrets)}")
                raise

            # Inbound forwarding and outbound event consumption are two independent
            # streams (inbound audio keeps arriving from the caller regardless of
            # provider turn boundaries) - they must run as real concurrent tasks,
            # not a fire-and-forget task raced against a synchronous loop. A bare
            # `asyncio.ensure_future(...)` followed by an immediate `.cancel()` on
            # loop exit never gives the forwarding task a chance to actually run
            # if nothing in the receive loop truly suspends the event loop first -
            # asyncio.wait() below is what forces that handoff.
            resumed = bool(resume_handle) and session.resumed
            if resume_handle:
                if resumed:
                    logger.info(f"voice call {fingerprint(ticket)}: resumed the earlier conversation")
                else:
                    # An unknown or expired id raises no error on the provider: the call is simply fresh.
                    logger.warning(f"voice call {fingerprint(ticket)}: resume: not honoured, opening fresh")
            if resumed:
                # The earlier conversation, caller opening included, is already in the provider's context.
                await session.submit_message("system", _RESUME_NOTE)
            else:
                if self._caller_opening and state.turn_ownership is TurnOwnership.RELAY:
                    # A user turn, not system text: live, the caller's own request changed how
                    # Lelik spoke where the same rule as system text did not (2026-09-25).
                    await session.submit_message("user", self._caller_opening)
                pickup_note = _WEB_PICKUP_NOTE if config.get("call_kind") == "web" else _PICKUP_NOTE
                await session.submit_message("system", pickup_note)
            await self._reply_to_turn(session, state, "pickup")

            forward_task = asyncio.ensure_future(self._forward_inbound(session, inbound_audio))
            consume_task = asyncio.ensure_future(
                self._consume_events(ticket, config, session, buffer, send_outbound_audio, clear_outbound_audio, state,
                                     secrets=secrets)
            )
            # The watchdog never ends on its own; the call ends with the audio streams.
            if state.turn_ownership is TurnOwnership.RELAY:
                watchdog_task = asyncio.ensure_future(self._watch_silence(session, state))
            if self._thinking_cue and send_cue_audio is not None:
                cue_task = asyncio.ensure_future(self._play_cue(state, send_cue_audio))
            # The watchdog ends only to hang up after a long silence (hangup_after_silence_s).
            call_loops = {task for task in (forward_task, consume_task, watchdog_task) if task is not None}
            await asyncio.wait(call_loops, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # 1. The call's own loops first, so nothing (the watchdog, a flush) starts a response
            #    on a call that is ending.
            loops = [task for task in (forward_task, consume_task, watchdog_task, cue_task, control_task)
                     if task is not None]
            if state.pending_barge_in is not None:
                loops.append(state.pending_barge_in)
            await self._cancel_all(ticket, loops, secrets=secrets)
            # A pressed or busy "Ask Lelik" button is released (`ask_state{idle, call_ended}`) before the `bye`.
            if state.ask is not None and control_link is not None:
                self._apply_ask(state, state.ask.end())
                await self._flush_ask(control_link, state)
            # The page learns the call ended (`bye{call_ended}`); its token is forgotten. Never raises.
            if control_link is not None and self._control_hub is not None:
                await self._control_hub.close(control_link)
            # 2. Abandon what the caller will not hear, before the delegations are cancelled: the
            #    main service posts those answers to the user's chat. Concurrently, so teardown
            #    waits one bound (_ABANDON_TIMEOUT_S), not one per delegation.
            abandoned = [call_id for task, call_id in state.pending_delegations.items() if not task.done()]
            # Arrived while a response was active and never spoken; timeouts and failures carry no answer.
            abandoned += [answer.call_id for answer in state.queued_answers
                          if answer.output not in (_DELEGATION_TIMED_OUT, _DELEGATION_FAILED)]
            if abandoned:
                logger.info(f"voice call {fingerprint(ticket)}: call ended with {len(abandoned)} unheard answers, abandoning")
                await asyncio.gather(*(self._abandon(ticket, call_id) for call_id in abandoned))
            # 3. In-flight delegations stop here: the abandoned ones reach chat from the main service.
            await self._cancel_all(ticket, list(state.delegations) + list(state.errands))
            # 4. Close the provider session, then report the call end. The report also revokes the
            #    call's voice MCP tokens (LELIK_VOICE_MCP_RFC §5.3), so a close that raises must not
            #    skip it: logged here, re-raised after the report.
            try:
                if session is not None:
                    await session.close()
            except Exception as exc:
                logger.error(f"voice call {fingerprint(ticket)}: closing the realtime session failed: "
                             f"{type(exc).__name__}", exc_info=True)
                raise
            finally:
                await self._control_plane.submit_transcript(
                    call_id=ticket,
                    user_id=config["user_id"],
                    account_id=config["account_id"],
                    buffer=buffer,
                    # Only a provider that issued a handle adds the arguments (other calls report exactly as before).
                    **({"resume_handle": state.resume_handle, "resume_provider": spec.provider}
                       if state.resume_handle else {}),
                )

    async def _cancel_all(self, ticket: str, tasks: List[asyncio.Task], *, secrets: Sequence[str] = ()) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            # Logged, not re-raised: this is a top-level call loop with no
            # caller to propagate to - the call simply ends here, which is
            # the correct outcome for a crashed forward/consume loop. The
            # "error" event path above already alerts on provider-side
            # failures; this only catches an unexpected bug in the loop
            # itself, and still needs to be visible in logs.
            if isinstance(result, Exception):
                logger.error(f"voice call {fingerprint(ticket)}: session loop crashed: "
                             f"{redact_exception(result, secrets=secrets)}")

    # ------------------------------------------------------------------ "Ask Lelik" (VOICE_CALL_CONTROL_CHANNEL_RFC §5)

    async def _run_ask_control(self, link: "CallControlLink", state: _CallState) -> None:
        """The control task of an interpreter call: page commands drive the latch, its timeouts are checked on every
        beat, and every `ask_state` (its own and the consume loop's) goes to the page. Never ends on its own; the
        call's teardown cancels it."""
        latch = state.ask
        if latch is None:
            return
        while True:
            try:
                command: Optional[dict] = await asyncio.wait_for(link.commands.get(), timeout=self._ask_tick_s)
            except asyncio.TimeoutError:
                command = None
            if command is not None:
                before = latch.state
                seq = command_seq(command)
                if command.get("type") == ASK_ARM:
                    step = latch.arm(seq)
                elif command.get("type") == ASK_DISARM:
                    step = latch.disarm(seq)
                else:
                    step = AskStep()
                if before is AskState.IDLE and latch.state is AskState.ARMED:
                    logger.info(f"voice call {fingerprint(state.ticket)}: ask: armed")
                self._apply_ask(state, step)
            self._apply_ask(state, latch.tick())
            await self._flush_ask(link, state)

    async def _mark_ask(self, session: RealtimeSessionPort, state: _CallState, step: AskStep) -> None:
        if step.insert_note:
            await session.submit_message("system", _ASK_NOTE)
            logger.info(f"voice call {fingerprint(state.ticket)}: ask: note inserted")
        self._apply_ask(state, step)

    @staticmethod
    def _apply_ask(state: _CallState, step: AskStep) -> None:
        """Record a step's report for the page: the state a reconnecting page gets in `welcome`, and an `ask_state`
        frame for the control task to send. Synchronous: the consume loop never waits on the page's socket."""
        report = step.report
        if report is None or state.ask_link is None:
            return
        state.ask_link.set_state(ask=report.state)
        reason = report.reason.value if report.reason is not None else None
        state.ask_outbox.append(ask_state_frame(report.state, seq=report.seq, reason=reason))
        if report.reason is AskReason.CANCELLED:
            logger.info(f"voice call {fingerprint(state.ticket)}: ask: cancelled")
        elif report.reason is not None:
            logger.info(f"voice call {fingerprint(state.ticket)}: ask: released ({reason})")

    @staticmethod
    async def _flush_ask(link: "CallControlLink", state: _CallState) -> None:
        frames, state.ask_outbox = state.ask_outbox, []
        for frame in frames:
            await link.send(frame)  # never raises; a frame lost with the socket is re-synced by the next `welcome`

    async def _consume_events(
        self,
        ticket: str,
        config: dict,
        session: RealtimeSessionPort,
        buffer: VoiceCallBuffer,
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
        clear_outbound_audio: Callable[[], Awaitable[None]],
        state: _CallState,
        *,
        secrets: Sequence[str] = (),
    ) -> None:
        turn_start: Optional[datetime] = None
        pending_request_text = ""
        pending_response_text = ""
        tool_called = False  # Per response: this or a barge-in explains an empty transcript.
        async for event in session.receive_events():
            if event.type == "response_created":
                if not state.response_active:
                    # Only a provider that owns turns starts a reply the relay did not claim.
                    state.response_reason = "provider"
                    state.response_audible = False
                    state.response_started_at = asyncio.get_running_loop().time()
                turn_start = datetime.now(timezone.utc)
                state.response_active = True
                state.response_cancelled = False
                tool_called = False
                if state.ask is not None:
                    self._apply_ask(state, state.ask.response_created())
            elif event.type == "speech_started":
                if state.ask is not None:
                    # First, before anything else is sent: a note created now lands right before the caller's turn
                    # (xAI probe, RFC §5.2), which is committed a second or more later.
                    await self._mark_ask(session, state, state.ask.speech_started(event.payload.get("item_id")))
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
                state.speech_in_greeting_guard = False
                if state.response_active or not state.playback.caught_up:
                    if self._in_greeting_guard(state):
                        state.speech_in_greeting_guard = True
                    elif state.turn_ownership is TurnOwnership.PROVIDER:
                        # The provider stops its own reply; what it already sent is still queued here.
                        await clear_outbound_audio()
                        logger.info(f"voice call {fingerprint(ticket)}: caller spoke over the reply, queued audio dropped")
                    elif self._barge_in_min_speech_s > 0:
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
                if state.ask is not None:
                    self._apply_ask(state, state.ask.speech_stopped(event.payload.get("item_id")))
                state.caller_speaking = False
                if state.speech_in_greeting_guard:
                    state.speech_in_greeting_guard = False
                    state.speech_dismissed = True
                    logger.info(f"voice call {fingerprint(ticket)}: speech during the greeting's first seconds, not an interruption")
                elif state.pending_barge_in is not None and not state.pending_barge_in.done():
                    state.pending_barge_in.cancel()
                    state.pending_barge_in = None
                    state.speech_dismissed = True
                    logger.info(f"voice call {fingerprint(ticket)}: speech too short to interrupt, Lelik keeps talking")
            elif event.type == "resume_handle":
                # Kept for the end-of-call report, never logged (VOICE_CALL_CONTROL_CHANNEL_RFC §9).
                state.resume_handle = event.payload["handle"]
            elif event.type == "turn_committed":
                if state.turn_ownership is TurnOwnership.PROVIDER:
                    # The provider is already replying (it starts before the commit arrives).
                    state.speech_dismissed = False
                elif state.speech_dismissed:
                    # Lelik is still mid-reply; the words stay in the conversation for his next turn.
                    state.speech_dismissed = False
                    logger.info(f"voice call {fingerprint(ticket)}: short turn over Lelik not answered")
                else:
                    await self._reply_to_turn(session, state, "turn")
            elif event.type == "audio_delta":
                if not state.response_audible:
                    state.response_audible = True
                    if state.response_reason == "pickup":
                        state.greeting_audible_at = asyncio.get_running_loop().time()
                    elapsed_ms =round((asyncio.get_running_loop().time() - state.response_started_at) * 1000)
                    logger.info(f"voice call {fingerprint(ticket)}: first audio after {elapsed_ms} ms ({state.response_reason})")
                    if self._cue_tail_queued(state):
                        # Only cue audio can be queued here (the cue plays only once playback caught up).
                        await clear_outbound_audio()
                    state.cue_tail_until = 0.0
                item_id = event.payload.get("item_id")
                if item_id != state.item_id:
                    state.item_id = item_id
                    state.item_start_bytes = state.playback.sent_bytes
                if state.ask is not None:
                    self._apply_ask(state, state.ask.reply_audio())
                await send_outbound_audio(event.payload["frame"])
            elif event.type == "tool_call":
                tool_called = True
                if state.response_cancelled:
                    call_id = event.payload.get("call_id")
                    logger.info(f"voice call {fingerprint(ticket)}: delegation {call_id} skipped (response cancelled)")
                    await session.submit_tool_result(call_id, _CANCELLED_TOOL_CALL)
                else:
                    self._start_delegation(ticket, config, session, state, buffer, event.payload, pending_request_text)
            elif event.type == "user_transcript":
                pending_request_text += event.payload["text"]
            elif event.type == "model_transcript":
                pending_response_text += event.payload["text"]
            elif event.type == "response_done":
                state.response_active = False
                if state.ask is not None:
                    self._apply_ask(state, state.ask.response_done())
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
                    logger.info(f"voice call {fingerprint(ticket)}: response done with an empty transcript")
                pending_request_text = ""
                pending_response_text = ""
                # No dispatch filler after a delegating turn (removed 2026-09-29, RFC §4.15): the
                # turn's own acknowledgement ("checking") already tells the caller, and the extra
                # reply voiced a second "it's on its way". A long wait still gets the watchdog's
                # _WAITING_NOTE after _silence_timeout_s.
                await self._flush_answers(session, state)
            elif event.type == "remote_tools_failed":
                # The provider could not list one server's tools (a refused token among others): the call goes on
                # without them. The label only, and the provider's text with any URL or token stripped (§5.4).
                logger.warning(f"voice call {fingerprint(ticket)}: remote tool server {event.payload.get('label')} "
                               f"unavailable, the call goes on without it: "
                               f"{redact_for_log(event.payload.get('reason', ''), secrets=secrets)}")
            elif event.type == "error":
                # The provider may echo the session payload: redacted again here, whatever the adapter did.
                message = redact_for_log(event.payload.get("message"), secrets=secrets)
                logger.error(f"voice call {fingerprint(ticket)}: provider error {message}")
                await self._alert_sink.post(f"Voice call {fingerprint(ticket)} provider error: {message}")
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
        logger.info(f"voice call {fingerprint(state.ticket)}: barge-in, heard {heard_ms} ms, cancelled={cancelled}")
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
                logger.error(f"voice call {fingerprint(state.ticket)}: barge-in failed: {redact_exception(exc)}",
                             exc_info=True)
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
            logger.warning(f"voice call {fingerprint(state.ticket)}: a response is already active, not starting another ({reason})")
            return
        state.response_active = True
        state.response_owed = False
        state.response_reason = reason
        state.response_audible = False
        state.response_started_at = asyncio.get_running_loop().time()
        logger.info(f"voice call {fingerprint(state.ticket)}: starting response ({reason})")
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
        waiting_gap = self._next_waiting_gap()
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
                logger.info(f"voice call {fingerprint(state.ticket)}: hanging up after {round(now - quiet_since)} s of silence")
                return
            limit = waiting_gap if state.delegations else self._silence_timeout_s
            if (state.silence_prompted and not state.delegations) or now - quiet_since < limit:
                continue
            state.silence_prompted = True
            quiet_since = now
            if state.delegations:
                waiting_gap = self._next_waiting_gap()
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
                    logger.info(f"voice call {fingerprint(state.ticket)}: cue stopped after {round((now - started_at) * 1000)} ms")
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
                logger.info(f"voice call {fingerprint(state.ticket)}: cue started ({kind})")
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
        # The session's two tools run as one kind of delegation: send_to_chat becomes its errand intent here.
        arguments = delegation_arguments(payload.get("name"), arguments)
        logger.info(f"voice call {fingerprint(ticket)}: delegation {payload.get('call_id')} dispatched ({arguments.get('intent')})")
        # Tracked, never fire-and-forget (RUF006): the call's teardown cancels what is left.
        task = asyncio.ensure_future(self._run_delegation(
            ticket, config, session, state, payload.get("call_id"),
            arguments, call_context, dispatched_at,
        ))
        if is_errand(arguments):
            # Only the acknowledgement comes back, in well under a second: nothing to host a wait
            # for, and no answer to post to chat if the call ends first — the errand itself runs
            # on as a Cloud Task either way. UAT 2026-09-29: the filler made Lelik say it twice.
            state.errands.add(task)
            task.add_done_callback(state.errands.discard)
            return
        state.delegations.add(task)
        task.add_done_callback(state.delegations.discard)
        state.pending_delegations[task] = payload.get("call_id")
        task.add_done_callback(lambda done: state.pending_delegations.pop(done, None))

    async def _run_delegation(
        self, ticket: str, config: dict, session: RealtimeSessionPort, state: _CallState,
        call_id: str, arguments: dict, call_context: list, dispatched_at: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            output = await self._fetch_answer(ticket, config, state, call_id, arguments, call_context)
            elapsed_ms = round((loop.time() - started) * 1000)
            answer = _Answer(call_id, output, dispatched_at, _request_label(arguments))
            await self._resolve_late_answer(session, state, answer, elapsed_ms)
        except asyncio.CancelledError:
            logger.info(f"voice call {fingerprint(ticket)}: call ended, delegation {call_id} discarded")
            raise
        except Exception as exc:
            # Nothing gathers a finished delegation task, so an error here is logged or lost.
            logger.error(f"voice call {fingerprint(ticket)}: answer to delegation {call_id} not delivered: "
                         f"{redact_exception(exc)}", exc_info=True)

    async def _fetch_answer(
        self, ticket: str, config: dict, state: _CallState, call_id: str, arguments: dict, call_context: list,
    ) -> str:
        try:
            return await asyncio.wait_for(
                self._control_plane.delegate(
                    user_id=config["user_id"], account_id=config["account_id"],
                    arguments=arguments, call_context=call_context,
                    ticket=ticket, call_id=call_id, request=_request_label(arguments),
                ),
                timeout=self._delegation_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(f"voice call {fingerprint(ticket)}: delegation {call_id} timed out, its answer goes to chat")
            # Still pending until the abandon has landed: a call ending mid-abandon (which cancels
            # this task) abandons it again from teardown. Twice is harmless; never is a lost answer.
            await self._abandon(ticket, call_id)
            state.pending_delegations.pop(asyncio.current_task(), None)
            return _DELEGATION_TIMED_OUT
        except Exception as exc:
            # No retry: it re-runs the whole pipeline (double spend, double chat copy).
            logger.error(f"voice call {fingerprint(ticket)}: delegation {call_id} failed: {redact_exception(exc)}")
            return _DELEGATION_FAILED
        finally:
            # The answer (or its failure) is in the relay's hands: nothing left to abandon.
            state.pending_delegations.pop(asyncio.current_task(), None)

    async def _abandon(self, ticket: str, call_id: str) -> None:
        # The port promises never to raise; bounded and caught here anyway, since this runs in
        # call teardown and a slow OIDC token fetch is outside the adapter's own HTTP timeout.
        try:
            await asyncio.wait_for(self._control_plane.abandon_delegation(ticket, call_id), _ABANDON_TIMEOUT_S)
        except Exception as exc:
            logger.warning(f"voice call {fingerprint(ticket)}: abandoning delegation {call_id} failed: "
                           f"{redact_exception(exc)}")

    async def _resolve_late_answer(
        self, session: RealtimeSessionPort, state: _CallState, answer: _Answer, elapsed_ms: int,
    ) -> None:
        # The one seam RFC §4.7 names; swap the policy here, nowhere else.
        if state.response_active:
            # response.create during an active response is a call-ending provider error.
            logger.info(f"voice call {fingerprint(state.ticket)}: answer {answer.call_id} arrived after {elapsed_ms} ms, queued")
            state.queued_answers.append(answer)
            return
        logger.info(f"voice call {fingerprint(state.ticket)}: answer {answer.call_id} arrived after {elapsed_ms} ms, injecting")
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
        logger.info(f"voice call {fingerprint(state.ticket)}: answer {answer.call_id} injected as {channel}, "
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
        logger.info(f"voice call {fingerprint(state.ticket)}: flushing {len(queued)} queued answers")
        await self._deliver_answers(session, state, queued)
