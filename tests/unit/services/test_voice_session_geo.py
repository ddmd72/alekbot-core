"""The `geo` command of a web call through the real VoiceSessionService, hub and `/ctl` handler (GEO_MARKS_RFC §4.7):
the page sends one fix, the call's provider session gets one system note with the rendered mark, and the page is told
with `geo_state`. The provider is a fake that keeps its items in order."""
import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncIterator, List, Optional

import pytest

from src.domain.geo_mark import GeoFix, GeoMark, GeoPlace, GeoSource, render_call_note
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_turn_ownership import TurnOwnership
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent, RealtimeSessionPort
from src.ports.reverse_geocoding_port import ReverseGeocodingPort
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import _ASK_NOTE, VoiceSessionService, VoiceControl
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0001-invented"
TICKET = "ticket-0001-invented"
ADDRESS = "12 Oak Street, Springfield"
_SETTLE_S = 0.03


class _Provider(RealtimeSessionPort):
    supported_turn_ownership = frozenset({TurnOwnership.PROVIDER})

    def __init__(self, open_gate: Optional[asyncio.Event] = None) -> None:
        self.events: "asyncio.Queue[Optional[RealtimeSessionEvent]]" = asyncio.Queue()
        self.conversation: List[tuple] = []
        self._open_gate = open_gate

    async def open(self, instructions: str, reasoning_effort: str, tools: List[Any]) -> None:
        if self._open_gate is not None:
            await self._open_gate.wait()

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
        self.conversation.append((role, text))

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


class _Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


class _Geocoder(ReverseGeocodingPort):
    def __init__(self, address: Optional[str] = ADDRESS, raises: bool = False) -> None:
        self.address, self.raises, self.asked = address, raises, []

    async def look_up(self, latitude: float, longitude: float) -> GeoPlace:
        self.asked.append((latitude, longitude))
        if self.raises:
            raise RuntimeError("geocoder down")
        return GeoPlace(self.address, (), ())


class _Call:
    def __init__(self, mode: Optional[str], geocoder: Optional[ReverseGeocodingPort],
                 open_gate: Optional[asyncio.Event] = None) -> None:
        self.hub = CallControlHub()
        self.provider = _Provider(open_gate)
        self.clock = _Clock()
        self.ends = asyncio.Event()
        config = {"instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [],
                  "call_kind": "web", "voice": VOICE_PROVIDER_PROFILES["xai"].session.to_dict(),
                  "control_token_sha256": control_token_digest(TOKEN)}
        if mode:
            config["mode"] = mode
        self.service = VoiceSessionService(
            realtime_session_factory=lambda spec: self.provider, control_plane=_ControlPlane(config),
            alert_sink=_Alerts(), control=VoiceControl(self.hub, geocoder), ask_clock=self.clock, ask_tick_s=0.005)
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

    async def page_says(self, frame: dict) -> None:
        self.page.page_says(frame)
        await asyncio.sleep(_SETTLE_S)

    def geo_states(self) -> List[dict]:
        return [frame for frame in self.page.frames() if frame["type"] == "geo_state"]

    def geo_notes(self) -> List[str]:
        return [text for role, text in self.provider.conversation if role == "system" and "The caller has just shared" in text]

    async def end(self) -> None:
        self.ends.set()
        await asyncio.wait_for(asyncio.gather(*self._tasks), timeout=2)


async def _noop(*args: Any) -> None:
    pass


def _geo(seq: int = 1, **overrides) -> dict:
    return {"type": "geo", "seq": seq, "latitude": 41.1, "longitude": -8.6, "accuracy": 40, **overrides}


def _expected_note(address: Optional[str]) -> str:
    fix = GeoFix.parse(41.1, -8.6, 40, datetime.now(timezone.utc), GeoSource.LELIK_BUTTON)
    return render_call_note(GeoMark(fix=fix, address=address))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "tutor", "interpreter"])
async def test_a_geo_frame_inserts_one_note_with_the_rendered_mark_and_answers_geo_state(mode):
    geocoder = _Geocoder()
    call = await _Call(mode, geocoder).start()
    await call.page_says(_geo())
    assert call.geo_notes() == [_expected_note(ADDRESS)] and ADDRESS in call.geo_notes()[0]
    assert call.geo_states() == [{"type": "geo_state", "ok": True, "seq": 1, "address": ADDRESS, "accuracy": 40.0}]
    assert geocoder.asked == [(41.1, -8.6)]
    await call.end()


@pytest.mark.asyncio
async def test_every_web_call_with_a_link_offers_geo():
    for mode, features in [(None, ["geo"]), ("tutor", ["geo", "text"]), ("interpreter", ["ask", "geo", "text"])]:
        call = await _Call(mode, None).start()
        assert call.page.frames()[0]["features"] == features
        await call.end()


@pytest.mark.asyncio
async def test_a_second_fix_within_ten_seconds_is_refused_and_one_after_it_is_taken():
    call = await _Call(None, _Geocoder()).start()
    await call.page_says(_geo(1))
    call.clock.now += 9.9
    await call.page_says(_geo(2))
    assert call.geo_states()[1] == {"type": "geo_state", "ok": False, "seq": 2, "reason": "rate"}
    assert len(call.geo_notes()) == 1

    call.clock.now += 0.2
    await call.page_says(_geo(3))
    assert call.geo_states()[2]["ok"] is True and call.geo_states()[2]["seq"] == 3
    assert len(call.geo_notes()) == 2
    await call.end()


@pytest.mark.asyncio
async def test_without_a_geocoder_the_note_names_no_address_and_the_state_carries_none():
    call = await _Call(None, None).start()
    await call.page_says(_geo())
    assert call.geo_notes() == [_expected_note(None)] and "not available" in call.geo_notes()[0]
    assert call.geo_states() == [{"type": "geo_state", "ok": True, "seq": 1, "accuracy": 40.0}]
    await call.end()


@pytest.mark.asyncio
async def test_a_geocoder_that_finds_nothing_or_raises_still_shares_the_position():
    for geocoder in (_Geocoder(address=None), _Geocoder(raises=True)):
        call = await _Call(None, geocoder).start()
        await call.page_says(_geo())
        assert call.geo_notes() == [_expected_note(None)]
        assert call.geo_states()[0]["ok"] is True and "address" not in call.geo_states()[0]
        await call.end()


@pytest.mark.asyncio
async def test_a_bad_fix_is_refused_without_a_note_and_does_not_start_the_rate_window():
    call = await _Call(None, _Geocoder()).start()
    await call.page_says(_geo(1, latitude=95))
    await call.page_says(_geo(2))
    assert call.geo_states()[0] == {"type": "geo_state", "ok": False, "seq": 1, "reason": "bad"}
    assert call.geo_states()[1]["ok"] is True and len(call.geo_notes()) == 1
    await call.end()


@pytest.mark.asyncio
async def test_a_fix_before_the_provider_session_is_open_is_refused_not_ready():
    gate = asyncio.Event()
    call = await _Call(None, _Geocoder(), open_gate=gate).start()
    await call.page_says(_geo())
    assert call.geo_states() == [{"type": "geo_state", "ok": False, "seq": 1, "reason": "not_ready"}]
    assert call.provider.conversation == []
    gate.set()
    await asyncio.sleep(_SETTLE_S)
    await call.page_says(_geo(2))
    assert call.geo_states()[1]["ok"] is True
    await call.end()


@pytest.mark.asyncio
async def test_the_interpreters_ask_flow_is_unchanged_next_to_a_geo_frame():
    call = await _Call("interpreter", _Geocoder()).start()
    await call.page_says({"type": "ask_arm", "seq": 1})
    await call.page_says(_geo())
    await call.page_says({"type": "ask_disarm", "seq": 2})
    asks = [frame for frame in call.page.frames() if frame["type"] == "ask_state"]
    assert [(f["state"], f.get("seq")) for f in asks] == [("armed", 1), ("idle", 2)]
    assert len(call.geo_notes()) == 1 and _ASK_NOTE not in [t for _, t in call.provider.conversation]
    await call.end()
