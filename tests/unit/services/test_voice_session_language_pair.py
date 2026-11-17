"""The interpreter call's language pair through the real VoiceSessionService, hub and `/ctl` handler
(decisions/lelik_voice_modes.md § Interpreter behaviour): while an Ask cycle is open the relay offers the one tool
`set_language_pair`, code decides whether a call is accepted, an accepted pair goes into the session instructions and
every later translate note, and the tool is withdrawn when the cycle is over.

The provider is a fake that keeps one ordered `wire` of what the relay sent (session updates, tool results, messages,
response requests) and refuses to count a `response.create` sent while a response is active."""
import asyncio
import json
from typing import Any, AsyncIterator, List, Optional

import pytest

from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_language_pair import (
    PAIR_TOOL_ALREADY,
    PAIR_TOOL_BAD,
    PAIR_TOOL_REFUSED,
    LanguagePair,
    language_pair_tool_declaration,
)
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_turn_ownership import TurnOwnership
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import (
    _ASK_NOTE,
    _CANCELLED_TOOL_CALL,
    _TRANSLATE_NOTE,
    VoiceSessionService,
)
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0002-invented"
TICKET = "ticket-0002-invented"
BASE = "base instructions placeholder"
_SETTLE_S = 0.03
_DECL = language_pair_tool_declaration()
UK_FR = LanguagePair(caller="uk", other="fr")
EN_ES = LanguagePair(caller="en", other="es")


class _Provider(RealtimeSessionPort):
    """An xAI-like session that records, in order, everything the relay sends it."""

    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER, TurnOwnership.RELAY})
    late_tool_result_after_caller_turn = True

    def __init__(self) -> None:
        self.events: "asyncio.Queue[Optional[RealtimeSessionEvent]]" = asyncio.Queue()
        self.wire: List[tuple] = []
        self.active = False
        self.create_while_active = 0
        # Set by a test to hold open() / an update in flight; an update is on the wire only once its send returned.
        self.hold_open: Optional[asyncio.Event] = None
        self.hold_update: Optional[asyncio.Event] = None

    async def open(self, instructions: str, reasoning_effort: str, tools: List[Any]) -> None:
        if self.hold_open is not None:
            await self.hold_open.wait()
        self.wire.append(("open", instructions, list(tools)))

    async def send_audio(self, frame: AudioFrame) -> None:
        pass

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        while True:
            event = await self.events.get()
            if event is None:
                return
            if event.type == "response_created":
                self.active = True
            elif event.type == "response_done":
                self.active = False
            yield event

    async def update_session(self, instructions: Optional[str] = None, tools: Optional[Any] = None) -> None:
        if self.hold_update is not None:
            await self.hold_update.wait()
        self.wire.append(("update", instructions, None if tools is None else list(tools)))

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        self.wire.append(("tool_result", call_id, output))

    async def submit_message(self, role: str, text: str) -> None:
        self.wire.append(("message", role, text))

    async def request_response(self) -> None:
        if self.active:
            self.create_while_active += 1
        self.active = True
        self.wire.append(("response.create",))

    async def cancel_response(self) -> None:
        self.active = False
        self.wire.append(("response.cancel",))

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        pass

    async def close(self) -> None:
        pass

    # ---- reading the wire

    def updates(self) -> List[tuple]:
        return [entry[1:] for entry in self.wire if entry[0] == "update"]

    def tool_updates(self) -> List[list]:
        return [tools for _, tools in self.updates() if tools is not None]

    def instruction_updates(self) -> List[str]:
        return [instructions for instructions, _ in self.updates() if instructions is not None]

    def tool_results(self) -> List[tuple]:
        return [entry[1:] for entry in self.wire if entry[0] == "tool_result"]

    def system_notes(self) -> List[str]:
        return [entry[2] for entry in self.wire if entry[0] == "message" and entry[1] == "system"]

    def creates(self) -> int:
        return sum(1 for entry in self.wire if entry[0] == "response.create")

    def index(self, entry: tuple) -> int:
        return self.wire.index(entry)


class _ControlPlane:
    def __init__(self, config: dict) -> None:
        self._config = config
        self.delegated: List[dict] = []

    async def fetch_session_config(self, ticket: str) -> dict:
        return self._config

    async def delegate(self, **kwargs: Any) -> str:
        self.delegated.append(kwargs)
        return "placeholder answer"

    async def abandon_delegation(self, ticket: str, call_id: str) -> None:
        pass

    async def submit_transcript(self, **kwargs: Any) -> None:
        pass


class _Alerts:
    async def post(self, text: str) -> None:
        pass


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _noop(*args: Any) -> None:
    pass


class _Call:
    """One web call in `mode` with the page's control socket attached."""

    def __init__(self, mode: str = "interpreter", provider: str = "xai") -> None:
        self.hub = CallControlHub()
        self.provider = _Provider()
        self.clock = _Clock()
        self.ends = asyncio.Event()
        self.control_plane = _ControlPlane({
            "instructions": BASE, "user_id": "usr-1", "account_id": "acc-1", "tools": [],
            "call_kind": "web", "mode": mode, "voice": VOICE_PROVIDER_PROFILES[provider].session.to_dict(),
            "control_token_sha256": control_token_digest(TOKEN)})
        self.service = VoiceSessionService(
            realtime_session_factory=lambda spec: self.provider, control_plane=self.control_plane,
            alert_sink=_Alerts(), control_hub=self.hub, ask_clock=self.clock, ask_tick_s=0.005)
        self.page = FakeControlSocket()
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> "_Call":
        async def inbound():
            await self.ends.wait()
            return
            yield  # pragma: no cover

        self._tasks.append(asyncio.ensure_future(self.service.handle_call(
            ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=_noop, clear_outbound_audio=_noop,
            playback=PlaybackTracker())))
        self.page.page_says({"type": "hello", "v": 1, "token": TOKEN})
        self._tasks.append(asyncio.ensure_future(
            CallControlHandler(self.hub, link_wait_s=1.0).handle_connection(self.page)))
        await asyncio.sleep(_SETTLE_S)
        return self

    async def press(self, seq: int = 1) -> None:
        self.page.page_says({"type": "ask_arm", "seq": seq})
        await asyncio.sleep(_SETTLE_S)

    async def release(self, seq: int) -> None:
        self.page.page_says({"type": "ask_disarm", "seq": seq})
        await asyncio.sleep(_SETTLE_S)

    async def says(self, event_type: str, **payload: Any) -> None:
        self.provider.events.put_nowait(RealtimeSessionEvent(type=event_type, payload=payload))
        await asyncio.sleep(_SETTLE_S)

    async def utterance(self, item_id: str) -> None:
        await self.says("speech_started", item_id=item_id)
        await self.says("speech_stopped", item_id=item_id)
        await self.says("turn_committed", item_id=item_id)

    async def pair_call(self, call_id: str, caller: Any = "uk", other: Any = "fr", raw: Optional[str] = None) -> None:
        arguments = raw if raw is not None else json.dumps({"caller_language": caller, "other_language": other})
        await self.says("tool_call", call_id=call_id, name="set_language_pair", arguments=arguments)

    async def tick(self, seconds: float) -> None:
        self.clock.now += seconds
        await asyncio.sleep(_SETTLE_S)

    async def end(self) -> None:
        self.ends.set()
        await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=2)


async def _ask_and_set(call: _Call, item_id: str, call_id: str, caller: str = "uk", other: str = "fr",
                       seq: int = 1) -> None:
    """A press, the caller's words to Lelik, the provider's reply with the pair call, its end, the confirmation."""
    await call.press(seq)
    await call.utterance(item_id)
    await call.says("response_created")
    await call.pair_call(call_id, caller, other)
    await call.says("response_done")
    await call.says("response_created")  # the confirmation the relay asked for
    await call.says("response_done")


@pytest.mark.asyncio
async def test_the_session_opens_without_tools_and_a_press_offers_the_tool_before_the_ask_note():
    call = await _Call().start()
    assert call.provider.wire[0] == ("open", BASE, [])
    assert call.provider.tool_updates() == []

    await call.press()
    assert call.provider.tool_updates() == [[_DECL]]

    await call.says("speech_started", item_id="item-u1")
    await call.end()
    assert call.provider.index(("update", None, [_DECL])) < call.provider.index(("message", "system", _ASK_NOTE))


@pytest.mark.asyncio
async def test_the_ask_note_waits_for_an_offer_still_in_flight():
    call = await _Call().start()
    call.provider.hold_update = asyncio.Event()
    await call.press()                                      # the offer is being sent ...
    await call.says("speech_started", item_id="item-u1")   # ... when the caller starts speaking
    assert _ASK_NOTE not in call.provider.system_notes()

    call.provider.hold_update.set()
    await asyncio.sleep(_SETTLE_S)
    await call.end()
    assert call.provider.index(("update", None, [_DECL])) < call.provider.index(("message", "system", _ASK_NOTE))
    assert call.provider.tool_updates() == [[_DECL]]        # offered once, not once per task


@pytest.mark.asyncio
async def test_a_press_made_while_the_session_opens_is_offered_once_it_is_open():
    call = _Call()
    call.provider.hold_open = asyncio.Event()
    await call.start()
    await call.press()
    assert call.provider.wire == []

    call.provider.hold_open.set()
    await asyncio.sleep(_SETTLE_S)
    await call.end()
    assert call.provider.wire[0] == ("open", BASE, [])
    assert call.provider.tool_updates() == [[_DECL]]


@pytest.mark.asyncio
async def test_an_accepted_call_sets_the_pair_confirms_once_and_withdraws_the_tool():
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    creates_before = call.provider.creates()
    await call.pair_call("call-1")

    assert call.provider.tool_results() == [("call-1", UK_FR.tool_ok())]
    assert call.provider.instruction_updates() == [f"{BASE}\n\n{UK_FR.block()}"]
    assert call.provider.index(("tool_result", "call-1", UK_FR.tool_ok())) < call.provider.index(
        ("update", f"{BASE}\n\n{UK_FR.block()}", None))
    assert call.provider.creates() == creates_before  # the reply carrying the call is still active

    await call.says("response_done")
    assert call.provider.creates() == creates_before + 1  # one reply, requested once the floor is free
    assert call.provider.tool_updates() == [[_DECL]]      # the confirmation still has the tool on offer
    notes_at_reply = call.provider.system_notes()

    await call.says("response_created")
    await call.says("response_done")
    await call.end()

    assert call.provider.tool_updates() == [[_DECL], []]
    assert call.provider.system_notes() == notes_at_reply  # no extra note on the confirmation
    assert call.provider.create_while_active == 0


@pytest.mark.asyncio
async def test_every_later_translate_note_names_the_pair():
    call = await _Call().start()
    await _ask_and_set(call, "item-u1", "call-1")
    await call.utterance("item-u2")
    await call.utterance("item-u3")
    await call.end()

    notes = call.provider.system_notes()
    assert notes[-2:] == [UK_FR.note(), UK_FR.note()]
    assert _TRANSLATE_NOTE not in notes[notes.index(_ASK_NOTE):]


@pytest.mark.asyncio
async def test_without_a_pair_the_translate_note_is_unchanged_and_nothing_is_offered():
    call = await _Call().start()
    await call.utterance("item-u1")
    await call.end()
    assert call.provider.system_notes()[-1] == _TRANSLATE_NOTE
    assert call.provider.updates() == []


@pytest.mark.asyncio
async def test_a_call_with_no_marked_turn_since_the_press_is_refused_and_changes_nothing():
    call = await _Call().start()
    await call.says("response_created")    # Lelik is translating ...
    await call.press()
    await call.pair_call("call-1")         # ... and calls the tool before the caller said a word to him
    assert call.provider.tool_results() == [("call-1", PAIR_TOOL_REFUSED)]
    await call.says("response_done")
    assert call.provider.creates() == 2    # the greeting and the reply to the refusal

    await call.utterance("item-u1")        # the press still marks these words
    await call.says("response_created")
    await call.says("response_done")
    await call.utterance("item-u2")
    await call.end()

    assert call.provider.instruction_updates() == []
    assert call.provider.system_notes()[-1] == _TRANSLATE_NOTE
    assert call.provider.create_while_active == 0


@pytest.mark.asyncio
async def test_a_call_after_the_cycle_ended_is_refused():
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.says("response_done")       # answered: the cycle is over, the tool withdrawn
    assert call.provider.tool_updates() == [[_DECL], []]

    await call.utterance("item-u2")        # words for translation
    await call.says("response_created")
    await call.pair_call("call-1")
    await call.end()
    assert call.provider.tool_results() == [("call-1", PAIR_TOOL_REFUSED)]
    assert call.provider.instruction_updates() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("caller", "other", "raw"), [
    pytest.param("uk", "xx", None, id="unknown-code"),
    pytest.param("fr", "fr", None, id="same-code"),
    pytest.param(None, None, "{not json", id="bad-json"),
])
async def test_bad_codes_get_the_bad_code_error_and_change_nothing(caller, other, raw):
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.pair_call("call-1", caller, other, raw=raw)
    await call.says("response_done")
    await call.says("response_created")
    await call.says("response_done")
    await call.utterance("item-u2")
    await call.end()

    assert call.provider.tool_results() == [("call-1", PAIR_TOOL_BAD)]
    assert call.provider.instruction_updates() == []
    assert call.provider.system_notes()[-1] == _TRANSLATE_NOTE
    assert call.provider.tool_updates() == [[_DECL], []]


@pytest.mark.asyncio
async def test_a_second_call_after_an_acceptance_in_the_same_press_is_refused():
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.pair_call("call-1", "uk", "fr")
    await call.pair_call("call-2", "en", "es")
    await call.says("response_done")
    await call.says("response_created")
    await call.says("response_done")
    await call.utterance("item-u2")
    await call.end()

    assert call.provider.tool_results() == [("call-1", UK_FR.tool_ok()), ("call-2", PAIR_TOOL_ALREADY)]
    assert call.provider.instruction_updates() == [f"{BASE}\n\n{UK_FR.block()}"]
    assert call.provider.system_notes()[-1] == UK_FR.note()
    assert call.provider.create_while_active == 0


@pytest.mark.asyncio
async def test_a_cancelled_press_withdraws_the_tool():
    call = await _Call().start()
    await call.press(seq=1)
    await call.release(seq=2)
    await call.end()
    assert call.provider.tool_updates() == [[_DECL], []]


@pytest.mark.asyncio
async def test_a_press_that_times_out_withdraws_the_tool():
    call = await _Call().start()
    await call.press()
    await call.tick(60.0)
    await call.end()
    assert call.provider.tool_updates() == [[_DECL], []]


@pytest.mark.asyncio
async def test_a_refused_call_does_not_hold_the_tool_after_the_answer():
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.pair_call("call-1", "uk", "xx")
    await call.says("response_done")
    assert call.provider.tool_updates() == [[_DECL], []]  # no confirmation to wait for
    await call.end()


@pytest.mark.asyncio
async def test_a_later_press_changes_the_pair_in_the_instructions_and_the_notes():
    call = await _Call().start()
    await _ask_and_set(call, "item-u1", "call-1", "uk", "fr", seq=1)
    await _ask_and_set(call, "item-u2", "call-2", "en", "es", seq=2)
    await call.utterance("item-u3")
    await call.end()

    assert call.provider.tool_results() == [("call-1", UK_FR.tool_ok()), ("call-2", EN_ES.tool_ok())]
    # The block replaces the earlier one: always the opening instructions plus the current pair.
    assert call.provider.instruction_updates() == [f"{BASE}\n\n{UK_FR.block()}", f"{BASE}\n\n{EN_ES.block()}"]
    assert call.provider.system_notes()[-1] == EN_ES.note()
    assert call.provider.tool_updates() == [[_DECL], [], [_DECL], []]
    assert call.provider.create_while_active == 0


@pytest.mark.asyncio
async def test_another_mode_never_offers_the_tool_and_delegates_a_call_of_that_name():
    call = await _Call(mode="lelik").start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.pair_call("call-1")
    await call.says("response_done")
    await asyncio.sleep(_SETTLE_S)
    await call.end()

    assert call.provider.updates() == []
    assert [entry["call_id"] for entry in call.control_plane.delegated] == ["call-1"]
    assert all(output != UK_FR.tool_ok() for _, output in call.provider.tool_results())


@pytest.mark.asyncio
async def test_a_call_of_a_cancelled_response_keeps_the_cancelled_tool_output():
    # Relay-owned turns: the caller talking over Lelik cancels the response, and its tool call is not run.
    call = await _Call(provider="openai").start()
    await call.press()
    await call.utterance("item-u1")        # the relay starts the reply to the marked words
    await call.says("response_created")
    await call.says("speech_started", item_id="item-u2")  # the caller talks over it: barge-in
    await call.pair_call("call-1")
    await call.end()

    assert call.provider.tool_results() == [("call-1", _CANCELLED_TOOL_CALL)]
    assert call.provider.instruction_updates() == []


@pytest.mark.asyncio
async def test_a_second_call_in_the_same_press_gets_the_already_set_error_not_the_translation_one():
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.pair_call("call-1", "uk", "fr")
    await call.pair_call("call-2", "en", "es")
    await call.says("response_done")
    await call.says("response_created")
    await call.says("response_done")
    await call.utterance("item-u2")
    await call.end()

    assert call.provider.tool_results() == [("call-1", UK_FR.tool_ok()), ("call-2", PAIR_TOOL_ALREADY)]
    assert call.provider.instruction_updates() == [f"{BASE}\n\n{UK_FR.block()}"]
    assert call.provider.system_notes()[-1] == UK_FR.note()
    assert call.provider.create_while_active == 0


@pytest.mark.asyncio
async def test_the_same_code_twice_gets_the_two_different_codes_error():
    call = await _Call().start()
    await call.press()
    await call.utterance("item-u1")
    await call.says("response_created")
    await call.pair_call("call-1", "es", "es")
    await call.says("response_done")
    await call.end()

    assert call.provider.tool_results() == [
        ("call-1", "Tool error: the two languages must be two different codes from the list. Nothing changed.")]
    assert call.provider.instruction_updates() == []
