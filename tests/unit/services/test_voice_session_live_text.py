"""Live call text through the real VoiceSessionService, hub and `/ctl` handler (VOICE_CALL_LIVE_TEXT_RFC §4.2-§4.4):
the page's control socket receives `line` and `snapshot` frames while the provider is a fake queue (xAI-like, or
relay-owned for the RELAY barge-in). The transport is modelled on `SfuStreamHandler`: sending audio counts sent bytes,
and a clear marks everything sent as played, so a cut made AFTER the clear would visibly release unplayed text."""
import asyncio
import base64
import logging
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
from src.services.voice_session_service import VoiceSessionService
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0001-invented"
TICKET = "ticket-0001-invented"
_SETTLE_S = 0.03
_BYTES_PER_MS = 8  # PlaybackTracker's default: μ-law 8 kHz

CALLER_SAYS = "We have a booking under RM-55120."
LELIK_FIRST = "Abbiamo una prenotazione "
LELIK_SECOND = "con il codice "
LELIK_THIRD = "RM-55120."
ASK_QUESTION = "Lelik, how do I say thank you in Italian?"


class _Provider(RealtimeSessionPort):
    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER, TurnOwnership.RELAY})

    def __init__(self) -> None:
        self.events: "asyncio.Queue[Optional[RealtimeSessionEvent]]" = asyncio.Queue()

    async def open(self, instructions: str, reasoning_effort: str, tools: List[Any]) -> None:
        pass

    async def send_audio(self, frame: AudioFrame) -> None:
        pass

    async def receive_events(self) -> AsyncIterator[RealtimeSessionEvent]:
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        pass

    async def submit_message(self, role: str, text: str) -> None:
        pass

    async def update_session(self, instructions: Optional[str] = None, tools: Optional[Any] = None) -> None:
        raise NotImplementedError

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

    async def fetch_session_config(self, ticket: str) -> dict:
        return self._config

    async def submit_transcript(self, **kwargs: Any) -> None:
        pass


class _Alerts:
    async def post(self, text: str) -> None:
        pass


class _Call:
    """One web call in `mode` with the page's control socket attached."""

    def __init__(self, mode: str, provider: str = "xai") -> None:
        self.hub = CallControlHub()
        self.provider = _Provider()
        self.playback = PlaybackTracker()
        self.ends = asyncio.Event()
        self.item = "item-a8"
        config = {
            "instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [],
            "call_kind": "web", "mode": mode, "voice": VOICE_PROVIDER_PROFILES[provider].session.to_dict(),
            "control_token_sha256": control_token_digest(TOKEN)}
        self.service = VoiceSessionService(
            realtime_session_factory=lambda spec: self.provider, control_plane=_ControlPlane(config),
            alert_sink=_Alerts(), control_hub=self.hub, ask_tick_s=0.005, live_text_tick_s=0.005)
        self.page = FakeControlSocket()
        self._tasks: List[asyncio.Task] = []

    async def start(self) -> "_Call":
        async def inbound():
            await self.ends.wait()
            return
            yield  # pragma: no cover

        async def send_outbound(frame: AudioFrame) -> None:
            self.playback.record_sent(frame.payload)

        async def clear_outbound() -> None:
            # `SfuStreamHandler._clear`: nothing queued, all sent "played".
            self.playback.record_played(str(self.playback.sent_bytes))

        self._tasks.append(asyncio.ensure_future(self.service.handle_call(
            ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=send_outbound,
            clear_outbound_audio=clear_outbound, playback=self.playback)))
        await self.attach(self.page)
        return self

    async def attach(self, page: FakeControlSocket) -> None:
        page.page_says({"type": "hello", "v": 1, "token": TOKEN})
        self._tasks.append(asyncio.ensure_future(CallControlHandler(self.hub, link_wait_s=1.0).handle_connection(page)))
        await asyncio.sleep(_SETTLE_S)

    async def page_says(self, frame: dict) -> None:
        self.page.page_says(frame)
        await asyncio.sleep(_SETTLE_S)

    async def says(self, event_type: str, **payload: Any) -> None:
        self.provider.events.put_nowait(RealtimeSessionEvent(type=event_type, payload=payload))
        await asyncio.sleep(_SETTLE_S)

    async def audio(self, n_bytes: int, item_id: Optional[str] = None) -> None:
        frame = AudioFrame(encoding="g711_ulaw", sample_rate_hz=8000,
                           payload=base64.b64encode(bytes(n_bytes)).decode(), track="outbound")
        await self.says("audio_delta", frame=frame, item_id=item_id or self.item)

    async def delta(self, text: str, offset_ms: Optional[int] = None, item_id: Optional[str] = None) -> None:
        await self.says("model_transcript_delta", item_id=item_id or self.item, text=text, audio_offset_ms=offset_ms)

    async def play(self, played_bytes: int) -> None:
        self.playback.record_played(str(played_bytes))
        await asyncio.sleep(_SETTLE_S)

    async def end(self) -> None:
        self.ends.set()
        await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=2)


def _lines(page: FakeControlSocket) -> List[dict]:
    return [frame for frame in page.frames() if frame["type"] == "line"]


def _text_of(page: FakeControlSocket, line: str) -> str:
    return "".join(frame.get("text", "") for frame in _lines(page) if frame["line"] == line and frame["op"] != "end")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,features", [("interpreter", ["ask", "text"]), ("tutor", ["text"])])
async def test_mode_calls_offer_text_and_send_a_snapshot_right_after_welcome(mode, features):
    call = await _Call(mode).start()
    await call.end()
    frames = call.page.frames()
    assert frames[0]["type"] == "welcome" and frames[0]["features"] == features
    assert frames[1] == {"v": 1, "seq": 1, "type": "snapshot", "lines": []}


@pytest.mark.asyncio
async def test_a_lelik_call_sends_no_line_frames_and_no_snapshot():
    call = await _Call("lelik").start()
    await call.says("response_created")
    await call.audio(1600)
    await call.delta(LELIK_FIRST, 0)
    await call.says("response_done")
    await call.play(1600)
    await call.end()
    assert call.page.types() == ["welcome", "bye"]


@pytest.mark.asyncio
async def test_a_phrase_is_sent_only_once_the_played_bytes_reach_its_anchor():
    call = await _Call("tutor").start()
    await call.says("response_created")
    await call.audio(1600)
    await call.delta(LELIK_FIRST, 0)
    await call.delta(LELIK_SECOND, 100)   # 100 ms in = byte 800
    assert _text_of(call.page, "l1") == LELIK_FIRST

    await call.play(799)
    assert _text_of(call.page, "l1") == LELIK_FIRST
    await call.play(800)
    assert _text_of(call.page, "l1") == LELIK_FIRST + LELIK_SECOND
    await call.delta(LELIK_THIRD, 150)
    await call.says("response_done")
    assert _lines(call.page)[-1]["op"] == "append"   # not ended while text is waiting
    await call.play(1200)
    await call.end()
    assert _text_of(call.page, "l1") == LELIK_FIRST + LELIK_SECOND + LELIK_THIRD
    ends = [frame for frame in _lines(call.page) if frame["op"] == "end"]
    assert ends == [{"v": 1, "seq": ends[0]["seq"], "type": "line", "line": "l1", "op": "end", "status": "done"}]


@pytest.mark.asyncio
async def test_the_xai_order_still_gives_the_caller_the_lower_id():
    call = await _Call("tutor").start()
    await call.says("speech_started", item_id="item-u7")
    await call.says("speech_stopped", item_id="item-u7")
    await call.says("response_created")             # xAI: the reply starts before the commit
    await call.says("turn_committed", item_id="item-u7")
    await call.audio(800)
    await call.delta(LELIK_FIRST, 0)
    await call.says("user_transcript", text=CALLER_SAYS, item_id="item-u7")   # and the transcript comes last
    await call.end()

    lines = _lines(call.page)
    caller = [frame for frame in lines if frame.get("who") == "caller"]
    lelik = [frame for frame in lines if frame.get("who") == "lelik"]
    assert [frame["line"] for frame in caller] == ["l1"] and caller[0]["text"] == CALLER_SAYS
    assert {frame["line"] for frame in lelik} == {"l2"}


@pytest.mark.asyncio
async def test_an_empty_transcript_is_never_sent():
    call = await _Call("tutor").start()
    await call.says("speech_stopped", item_id="item-u7")
    await call.says("user_transcript", text="  ", item_id="item-u7")
    await call.end()
    assert _lines(call.page) == []


@pytest.mark.asyncio
async def test_provider_barge_in_keeps_the_played_phrases_and_drops_the_rest():
    call = await _Call("tutor").start()
    await call.says("response_created")
    await call.audio(1600)
    await call.delta(LELIK_FIRST, 0)
    await call.delta(LELIK_SECOND, 50)     # byte 400
    await call.delta(LELIK_THIRD, 150)     # byte 1200
    await call.play(500)
    assert _text_of(call.page, "l1") == LELIK_FIRST + LELIK_SECOND

    # The caller talks over the reply: the cut reads played=500 BEFORE the clear marks all 1600 bytes as played.
    await call.says("speech_started", item_id="item-u9")
    await call.delta("never shown ", 160)     # a later delta of the cut reply
    await call.play(1600)
    await call.end()

    assert _text_of(call.page, "l1") == LELIK_FIRST + LELIK_SECOND
    assert [f["status"] for f in _lines(call.page) if f["op"] == "end"] == ["interrupted"]


@pytest.mark.asyncio
async def test_relay_barge_in_through_the_relay_barge_in_behaves_the_same_way():
    call = await _Call("tutor", provider="openai").start()
    await call.says("response_created")
    await call.audio(800)
    await call.delta(LELIK_FIRST)              # no offsets: anchored at the bytes sent so far
    await call.audio(800)
    await call.delta(LELIK_SECOND)
    await call.play(800)
    assert _text_of(call.page, "l1") == LELIK_FIRST

    await call.says("speech_started", item_id="item-u9")     # RELAY-owned: `_barge_in`
    await call.delta(LELIK_THIRD)
    await call.play(1600)
    await call.end()

    assert _text_of(call.page, "l1") == LELIK_FIRST
    assert [f["status"] for f in _lines(call.page) if f["op"] == "end"] == ["interrupted"]


@pytest.mark.asyncio
async def test_an_ask_exchange_marks_both_lines_and_the_next_exchange_does_not():
    call = await _Call("interpreter").start()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.says("speech_started", item_id="item-u7")
    await call.says("speech_stopped", item_id="item-u7")
    await call.says("turn_committed", item_id="item-u7")
    await call.says("response_created")
    await call.says("user_transcript", text=ASK_QUESTION, item_id="item-u7")
    await call.audio(800)
    await call.delta("Sim, fale português. ", 0)
    await call.says("response_done")
    await call.play(800)

    await call.says("speech_started", item_id="item-u8")
    await call.says("speech_stopped", item_id="item-u8")
    await call.says("response_created")
    await call.says("user_transcript", text=CALLER_SAYS, item_id="item-u8")
    await call.audio(800, item_id="item-a9")
    await call.delta(LELIK_FIRST, 0, item_id="item-a9")
    await call.play(1600)
    await call.end()

    by_line = {frame["line"]: frame for frame in _lines(call.page) if frame["op"] in ("set", "append")}
    assert [by_line[line].get("kind") for line in ("l1", "l2", "l3", "l4")] == ["ask", "ask", None, None]


@pytest.mark.asyncio
async def test_a_second_socket_gets_welcome_then_a_snapshot_above_every_earlier_seq():
    call = await _Call("tutor").start()
    await call.says("speech_stopped", item_id="item-u7")
    await call.says("user_transcript", text=CALLER_SAYS, item_id="item-u7")
    await call.says("response_created")
    await call.audio(1600)
    await call.delta(LELIK_FIRST, 0)
    await call.delta(LELIK_SECOND, 100)
    await call.play(0)
    earlier = max(frame["seq"] for frame in call.page.frames() if "seq" in frame)

    second = FakeControlSocket()
    await call.attach(second)
    await call.end()

    frames = second.frames()
    assert [frame["type"] for frame in frames[:2]] == ["welcome", "snapshot"]
    assert frames[1]["seq"] > earlier
    assert frames[1]["lines"] == [{"line": "l1", "who": "caller", "text": CALLER_SAYS},
                                  {"line": "l2", "who": "lelik", "text": LELIK_FIRST}]


@pytest.mark.asyncio
async def test_teardown_sends_the_final_frames_before_the_bye():
    call = await _Call("tutor").start()
    await call.says("response_created")
    await call.audio(1600)
    await call.delta(LELIK_FIRST, 0)
    await call.delta(LELIK_SECOND, 150)    # not played when the call ends
    await call.end()

    frames = call.page.frames()
    assert frames[-1]["type"] == "bye"
    assert frames[-2]["op"] == "end" and frames[-2]["status"] == "interrupted"
    assert _text_of(call.page, "l1") == LELIK_FIRST


@pytest.mark.asyncio
async def test_no_log_line_holds_a_phrase(caplog):
    caplog.set_level(logging.DEBUG)
    call = await _Call("interpreter").start()
    await call.says("speech_stopped", item_id="item-u7")
    await call.says("user_transcript", text=CALLER_SAYS, item_id="item-u7")
    await call.says("response_created")
    await call.audio(800)
    await call.delta(LELIK_FIRST, 0)
    await call.says("response_done")
    await call.play(800)
    await call.end()

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "live text: 2 lines, 1 phrases (1 with offsets), 1 released, 0 dropped" in logged
    for text in (CALLER_SAYS, LELIK_FIRST.strip(), "Albergo", "Abbiamo"):
        assert text not in logged


class _BlockingLineSocket(FakeControlSocket):
    """A page whose first `line` write never finishes: the beat is parked inside its send loop until cancelled."""

    parked = False

    async def send(self, message: str) -> None:
        if '"type":"line"' in message and not self.parked:
            self.parked = True
            await asyncio.sleep(3600)
        await super().send(message)


@pytest.mark.asyncio
async def test_a_batch_cut_short_by_the_teardown_cancel_is_still_fully_sent_in_order_before_the_bye():
    call = _Call("tutor")
    call.page = _BlockingLineSocket()
    await call.start()
    await call.says("speech_stopped", item_id="item-u7")
    await call.says("user_transcript", text=CALLER_SAYS, item_id="item-u7")
    await call.says("response_created")
    await call.audio(800)
    await call.delta(LELIK_FIRST, 0)      # one tick builds [caller set, lelik append]; the first write hangs
    assert _lines(call.page) == []

    await call.end()

    lines = _lines(call.page)
    assert [(f["line"], f["op"]) for f in lines] == [("l1", "set"), ("l2", "append"), ("l2", "end")]
    assert lines[2]["status"] == "interrupted"
    seqs = [f["seq"] for f in lines]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert call.page.frames()[-1]["type"] == "bye"
