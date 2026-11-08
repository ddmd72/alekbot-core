""""Ask Lelik" through the real VoiceSessionService, hub and `/ctl` handler (VOICE_CALL_CONTROL_CHANNEL_RFC §5): the
page presses over its control socket, the provider is a fake that keeps the conversation in order (a system item
lands where it is created; the caller's turn is added at commit, as on xAI), and the test reads what the provider's
conversation holds and what the page was told."""
import asyncio
from typing import Any, AsyncIterator, List, Optional

import pytest

from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_turn_ownership import TurnOwnership
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import _ASK_NOTE, _TRANSLATE_NOTE, _WEB_PICKUP_NOTE, VoiceSessionService
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0001-invented"
TICKET = "ticket-0001-invented"
_SETTLE_S = 0.03


class _Provider(RealtimeSessionPort):
    """An xAI-like session: provider-owned turns; `conversation` is the provider's item order."""

    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER})

    def __init__(self) -> None:
        self.events: "asyncio.Queue[Optional[RealtimeSessionEvent]]" = asyncio.Queue()
        self.conversation: List[tuple] = []

    async def open(self, instructions: str, reasoning_effort: str, tools: List[Any]) -> None:
        pass

    async def send_audio(self, frame: AudioFrame) -> None:
        pass

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        while True:
            event = await self.events.get()
            if event is None:
                return
            if event.type == "turn_committed":
                self.conversation.append(("user", event.payload["item_id"]))
            yield event

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        pass

    async def submit_message(self, role: str, text: str) -> None:
        self.conversation.append((role, text))

    async def request_response(self) -> None:
        pass

    async def cancel_response(self) -> None:
        pass

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        pass

    async def close(self) -> None:
        pass


class _ControlPlane:
    def __init__(self, config: dict) -> None:
        self._config = config
        self.transcripts = 0

    async def fetch_session_config(self, ticket: str) -> dict:
        return self._config

    async def submit_transcript(self, **kwargs: Any) -> None:
        self.transcripts += 1


class _Alerts:
    async def post(self, text: str) -> None:
        pass


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


class _Call:
    """One web call in `mode`, with the page's control socket attached."""

    def __init__(self, mode: str) -> None:
        self.hub = CallControlHub()
        self.provider = _Provider()
        self.clock = _Clock()
        self.ends = asyncio.Event()
        self.control_plane = _ControlPlane({
            "instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [],
            "call_kind": "web", "mode": mode, "voice": VOICE_PROVIDER_PROFILES["xai"].session.to_dict(),
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
        await self.attach(self.page)
        return self

    async def attach(self, page: FakeControlSocket) -> None:
        page.page_says({"type": "hello", "v": 1, "token": TOKEN})
        self._tasks.append(asyncio.ensure_future(CallControlHandler(self.hub, link_wait_s=1.0).handle_connection(page)))
        await asyncio.sleep(_SETTLE_S)

    async def page_says(self, frame: dict) -> None:
        self.page.page_says(frame)
        await asyncio.sleep(_SETTLE_S)

    async def provider_says(self, event_type: str, **payload: Any) -> None:
        self.provider.events.put_nowait(RealtimeSessionEvent(type=event_type, payload=payload))
        await asyncio.sleep(_SETTLE_S)

    async def tick_to(self, seconds: float) -> None:
        self.clock.now += seconds
        await asyncio.sleep(_SETTLE_S)

    def ask_states(self, page: Optional[FakeControlSocket] = None) -> List[dict]:
        return [frame for frame in (page or self.page).frames() if frame["type"] == "ask_state"]

    def notes(self) -> List[str]:
        return [text for role, text in self.provider.conversation if role == "system"]

    async def end(self) -> None:
        self.ends.set()
        await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=2)


async def _noop(*args: Any) -> None:
    pass


async def _interpreter_call() -> _Call:
    return await _Call("interpreter").start()


@pytest.mark.asyncio
async def test_the_note_goes_in_on_speech_started_while_armed_and_before_the_callers_turn():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.provider_says("speech_started", item_id="item-u1")
    assert call.notes() == [_WEB_PICKUP_NOTE, _ASK_NOTE]  # in before the words end, let alone commit

    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.end()

    assert call.provider.conversation == [("system", _WEB_PICKUP_NOTE), ("system", _ASK_NOTE), ("user", "item-u1")]
    assert call.ask_states()[:2] == [{"type": "ask_state", "state": "armed", "seq": 1},
                                     {"type": "ask_state", "state": "active"}]


@pytest.mark.asyncio
async def test_without_a_press_only_the_translate_note_is_sent():
    call = await _interpreter_call()
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.end()
    assert call.notes() == [_WEB_PICKUP_NOTE, _TRANSLATE_NOTE] and call.ask_states() == []


@pytest.mark.asyncio
async def test_a_cancel_before_speaking_sends_no_ask_note():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.page_says({"type": "ask_disarm", "seq": 2})
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.end()

    assert call.notes() == [_WEB_PICKUP_NOTE, _TRANSLATE_NOTE]
    assert call.ask_states() == [{"type": "ask_state", "state": "armed", "seq": 1},
                                 {"type": "ask_state", "state": "idle", "seq": 2, "reason": "cancelled"}]


@pytest.mark.asyncio
async def test_the_button_releases_on_the_reply_to_the_marked_words_not_on_the_reply_they_interrupted():
    call = await _interpreter_call()
    await call.provider_says("response_created")           # Lelik is mid-translation ...
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.provider_says("speech_started", item_id="item-u1")  # ... when the caller speaks over him
    await call.provider_says("response_done")              # xAI stops that reply: its done comes first
    assert call.ask_states()[-1] == {"type": "ask_state", "state": "active"}

    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.provider_says("response_created")           # Lelik's answer to the question
    assert call.ask_states()[-1] == {"type": "ask_state", "state": "active"}
    await call.provider_says("response_done")
    await call.end()

    assert call.ask_states() == [{"type": "ask_state", "state": "armed", "seq": 1},
                                 {"type": "ask_state", "state": "active"},
                                 {"type": "ask_state", "state": "idle", "reason": "answered"}]


@pytest.mark.asyncio
async def test_an_armed_button_releases_itself_after_sixty_seconds_without_speech():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.tick_to(59.5)
    assert call.ask_states()[-1]["state"] == "armed"
    await call.tick_to(0.5)
    assert call.ask_states()[-1] == {"type": "ask_state", "state": "idle", "reason": "timeout"}

    await call.provider_says("speech_started", item_id="item-u1")  # too late: the words are translated
    await call.end()
    assert call.notes() == [_WEB_PICKUP_NOTE, _TRANSLATE_NOTE]


@pytest.mark.asyncio
async def test_a_reconnecting_page_gets_the_current_state_in_welcome():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    second = FakeControlSocket()
    await call.attach(second)
    await call.end()
    assert second.frames()[0] == {"type": "welcome", "v": 1, "features": ["ask"], "ask": "armed"}
    assert call.page.frames()[-1] == {"type": "bye", "reason": "replaced"}


@pytest.mark.asyncio
async def test_the_call_ending_releases_a_pressed_button_before_the_bye():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.end()
    assert call.page.frames()[-2:] == [{"type": "ask_state", "state": "idle", "reason": "call_ended"},
                                       {"type": "bye", "reason": "call_ended"}]
    assert call.control_plane.transcripts == 1


@pytest.mark.asyncio
async def test_only_the_interpreter_call_offers_ask():
    call = await _interpreter_call()
    await call.end()
    assert call.page.frames()[0] == {"type": "welcome", "v": 1, "features": ["ask"], "ask": "idle"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lelik", "tutor"])
async def test_other_modes_offer_no_ask_and_ignore_a_press(mode):
    call = await _Call(mode).start()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.end()

    assert call.page.frames()[0] == {"type": "welcome", "v": 1, "features": []}
    assert call.ask_states() == [] and call.notes() == [_WEB_PICKUP_NOTE]


@pytest.mark.asyncio
async def test_a_long_answer_keeps_the_button_pressed_until_its_response_done():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.provider_says("turn_committed", item_id="item-u1")
    await call.provider_says("response_created")
    await call.tick_to(60.0)                               # Lelik is still answering a minute later
    assert call.ask_states()[-1] == {"type": "ask_state", "state": "active"}
    await call.provider_says("response_done")
    await call.end()
    assert call.ask_states()[-2:] == [{"type": "ask_state", "state": "active"},
                                      {"type": "ask_state", "state": "idle", "reason": "answered"}]


@pytest.mark.asyncio
async def test_a_reply_that_never_starts_releases_the_button_at_the_timeout():
    call = await _interpreter_call()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.provider_says("speech_started", item_id="item-u1")
    await call.provider_says("speech_stopped", item_id="item-u1")
    await call.tick_to(19.5)
    assert call.ask_states()[-1] == {"type": "ask_state", "state": "active"}
    await call.tick_to(0.5)
    await call.end()
    assert call.ask_states()[-1] == {"type": "ask_state", "state": "idle", "reason": "timeout"}
