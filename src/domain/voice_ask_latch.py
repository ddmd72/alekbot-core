"""AskLatch — the "Ask Lelik" state machine of one interpreter call (VOICE_CALL_CONTROL_CHANNEL_RFC §5.3).

Pure: no I/O, an injected clock. Every method is synchronous and returns an `AskStep` that says what the caller
(`VoiceSessionService`) must do: insert the system note now, and/or tell the page the new state. All transitions run
on the relay's one event loop, so the consume loop (speech and reply events) and the control task (page commands,
timeouts) never interleave inside one; the service performs the step after the transition.

| State          | Event                                         | Step -> next state                       |
|----------------|-----------------------------------------------|------------------------------------------|
| idle           | arm                                           | -> armed                                 |
| armed          | disarm                                        | -> idle, `cancelled` (nothing to delete) |
| armed          | speech_started(item)                          | note, remember item -> active            |
| armed          | 60 s with no speech                           | -> idle, `timeout`                       |
| active         | speech_stopped(item)                          | -> awaiting_reply                        |
| active         | speech_started (a pause split the question)   | note again                               |
| awaiting_reply | speech_started, no reply audio yet            | note again -> active                     |
| awaiting_reply | response_created                              | reply started                            |
| awaiting_reply | response_done after reply started             | -> idle, `answered`                      |
| awaiting_reply | 20 s from speech_stopped, no reply started    | -> idle, `timeout`                       |
| awaiting_reply | 90 s from the reply's start, no response_done | -> idle, `timeout` (a runaway reply)     |
| any but idle   | call ends                                     | -> idle, `call_ended`                    |

The button releases when Lelik's answer ends (owner), so once the reply has started no 20 s timeout can cut it
short, however long Lelik speaks; only the runaway cap remains. The 20 s timeout covers a reply that never starts
(a false VAD start, a dropped turn).

Two readings the table leaves open, decided here:
- A `response_done` before the reply to the marked words started belongs to the reply those words interrupted (xAI
  stops it and its `response.done` arrives before `speech_stopped`), so it never releases.
- Speech over Lelik's audible answer is not a continuation of the question: no second note, and the answer's own
  `response_done` (the interrupted one) releases with `answered`.

A page command that changes nothing (`arm` while armed, `disarm` while Lelik is answering, either while idle) still
gets the current state back with its `seq`: the page rolls a tap back unless an `ask_state` answers it within 2 s,
and `disarm` while active/awaiting must show the button busy (§5.3).

**Translate notes** (`translate_unmarked=True`, interpreter calls; owner, after the live test 2026-11-08: without the
button the model still answered phrases it took for questions to it). Every `speech_started` the latch does not mark
for Lelik asks for the translate note instead (`insert_translate_note`), so each caller or other-person turn carries an
explicit instruction either way. One `speech_started` yields at most one of the two notes. A translate note is given
once per utterance: a second `speech_started` for the same item id, or before the utterance's `speech_stopped`, gets
none (a VAD restart inside one utterance). A pause long enough to end the utterance starts a new item, which gets its
own note, as on the Ask path, since each committed turn needs the instruction right before it.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

ASK_ARMED_TIMEOUT_S = 60.0  # owner, Q1: an armed button with no speech releases itself
ASK_REPLY_TIMEOUT_S = 20.0  # from speech_stopped while no reply has started: a false VAD start, a dropped turn
ASK_RUNAWAY_REPLY_S = 90.0  # from the reply's response_created: a reply whose response_done never comes


class AskState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    ACTIVE = "active"                  # the marked words are being spoken
    AWAITING_REPLY = "awaiting_reply"  # the words ended; Lelik's answer is coming or playing

    @property
    def wire(self) -> str:
        """The state as the page knows it (`welcome.ask`, `ask_state.state`): idle, armed or active."""
        return "active" if self is AskState.AWAITING_REPLY else self.value


class AskReason(str, Enum):
    """Why the latch went back to idle (`ask_state.reason`)."""

    ANSWERED = "answered"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    CALL_ENDED = "call_ended"


@dataclass(frozen=True)
class AskReport:
    """An `ask_state` for the page: the state it shows now, the reason it went idle, the `seq` of the command answered."""

    state: str
    reason: Optional[AskReason] = None
    seq: Optional[int] = None


@dataclass(frozen=True)
class AskStep:
    """What the service does after a transition: insert the note (before anything else), then report to the page."""

    insert_note: bool = False
    report: Optional[AskReport] = None
    insert_translate_note: bool = False  # never together with insert_note


_NOTHING = AskStep()


class AskLatch:
    """One call's "Ask Lelik" latch. `clock` is monotonic seconds (the relay passes the event loop's time)."""

    def __init__(self, clock: Callable[[], float], armed_timeout_s: float = ASK_ARMED_TIMEOUT_S,
                 reply_timeout_s: float = ASK_REPLY_TIMEOUT_S, runaway_reply_s: float = ASK_RUNAWAY_REPLY_S,
                 translate_unmarked: bool = False) -> None:
        """`translate_unmarked`: speech the latch does not mark for Lelik asks for the translate note (module
        docstring)."""
        self._clock = clock
        self._translate_unmarked = translate_unmarked
        self._translated_item: Optional[str] = None
        self._translating = False  # a translate note went in and its utterance has not stopped yet
        self._armed_timeout_s = armed_timeout_s
        self._reply_timeout_s = reply_timeout_s
        self._runaway_reply_s = runaway_reply_s
        self._state = AskState.IDLE
        self._item_id: Optional[str] = None
        self._armed_at = 0.0
        self._stopped_at: Optional[float] = None
        self._reply_started_at: Optional[float] = None
        self._reply_audible = False

    @property
    def state(self) -> AskState:
        return self._state

    @property
    def item_id(self) -> Optional[str]:
        """The provider's id of the caller's turn the note marked last (None before the first note)."""
        return self._item_id

    # ------------------------------------------------------------------ page commands

    def arm(self, seq: Optional[int] = None) -> AskStep:
        if self._state is not AskState.IDLE:
            return self._echo(seq)
        self._state = AskState.ARMED
        self._armed_at = self._clock()
        return AskStep(report=AskReport(AskState.ARMED.wire, seq=seq))

    def disarm(self, seq: Optional[int] = None) -> AskStep:
        if self._state is not AskState.ARMED:
            # Idle: nothing to cancel. Active/awaiting: the words are already marked, so it is ignored.
            return self._echo(seq)
        return self._release(AskReason.CANCELLED, seq)

    # ------------------------------------------------------------------ provider events

    def speech_started(self, item_id: Optional[str]) -> AskStep:
        if self._state is AskState.ARMED or self._state is AskState.ACTIVE:
            return self._mark(item_id)
        if self._state is AskState.AWAITING_REPLY and not self._reply_audible:
            # A pause split the question before any answer was heard: the rest of it is marked too.
            return self._mark(item_id)
        return self._translate(item_id)

    def speech_stopped(self, item_id: Optional[str]) -> AskStep:
        self._translating = False
        if self._state is not AskState.ACTIVE:
            return _NOTHING
        if item_id is not None and self._item_id is not None and item_id != self._item_id:
            return _NOTHING  # the end of some other speech, not the marked one
        self._state = AskState.AWAITING_REPLY
        self._stopped_at = self._clock()
        self._reply_started_at = None
        self._reply_audible = False
        return _NOTHING

    def response_created(self) -> AskStep:
        if self._state is AskState.AWAITING_REPLY:
            self._reply_started_at = self._clock()
        return _NOTHING

    def reply_audio(self) -> AskStep:
        """Lelik's answer to the marked words is audible (its first audio arrived)."""
        if self._state is AskState.AWAITING_REPLY and self._reply_started_at is not None:
            self._reply_audible = True
        return _NOTHING

    def response_done(self) -> AskStep:
        if self._state is AskState.AWAITING_REPLY and self._reply_started_at is not None:
            return self._release(AskReason.ANSWERED)
        return _NOTHING

    # ------------------------------------------------------------------ time and teardown

    def tick(self) -> AskStep:
        """Called periodically. Releases an armed latch after `armed_timeout_s` with no speech; a marked one
        `reply_timeout_s` after the words ended when no reply has started; and a started reply only after
        `runaway_reply_s` with no response_done, so an answer is never cut short while Lelik is giving it."""
        now = self._clock()
        if self._state is AskState.ARMED and now - self._armed_at >= self._armed_timeout_s:
            return self._release(AskReason.TIMEOUT)
        if self._state is not AskState.AWAITING_REPLY:
            return _NOTHING
        if self._reply_started_at is not None:
            if now - self._reply_started_at >= self._runaway_reply_s:
                return self._release(AskReason.TIMEOUT)
        elif self._stopped_at is not None and now - self._stopped_at >= self._reply_timeout_s:
            return self._release(AskReason.TIMEOUT)
        return _NOTHING

    def end(self) -> AskStep:
        """The call ends: a latch that is not idle releases with `call_ended`."""
        if self._state is AskState.IDLE:
            return _NOTHING
        return self._release(AskReason.CALL_ENDED)

    # ------------------------------------------------------------------ internals

    def _mark(self, item_id: Optional[str]) -> AskStep:
        was = self._state
        self._state = AskState.ACTIVE
        self._item_id = item_id
        self._stopped_at = None
        self._reply_started_at = None
        self._reply_audible = False
        report = AskReport(AskState.ACTIVE.wire) if was.wire != AskState.ACTIVE.wire else None
        return AskStep(insert_note=True, report=report)

    def _translate(self, item_id: Optional[str]) -> AskStep:
        if not self._translate_unmarked or self._translating:
            return _NOTHING
        if item_id is not None and item_id == self._translated_item:
            return _NOTHING
        self._translating = True
        self._translated_item = item_id
        return AskStep(insert_translate_note=True)

    def _release(self, reason: AskReason, seq: Optional[int] = None) -> AskStep:
        self._state = AskState.IDLE
        self._stopped_at = None
        self._reply_started_at = None
        self._reply_audible = False
        return AskStep(report=AskReport(AskState.IDLE.wire, reason=reason, seq=seq))

    def _echo(self, seq: Optional[int]) -> AskStep:
        return AskStep(report=AskReport(self._state.wire, seq=seq))
