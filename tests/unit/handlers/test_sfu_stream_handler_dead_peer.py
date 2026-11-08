"""The relay's dead-peer watchdog (VOICE_CALL_CONTROL_CHANNEL_RFC §7.4, slice 6): a paired web call with no inbound
packet for N seconds ends itself through the normal end path, so the main side gets the call-end report, releases the
one-call marker and keeps the resume handle (the call did not end by the button).

The end-path test runs the real SfuStreamHandler, VoiceSessionService and HTTP control-plane client against the real
`/voice/submit-transcript` route over an in-process ASGI transport; only the provider session is a fake. Time is the
injected clock the gap tracker already uses.
"""
import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
from quart import Quart

import relay_main

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.domain.sfu_packet import encode_sfu_packet
from src.handlers.sfu_stream_handler import SfuStreamHandler, _SfuCall
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.services.voice_resume_service import VoiceResumeService
from src.services.voice_session_service import VoiceSessionService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.voice_tickets import DIGEST_KEY, call_kind_key, ticket_key

T1 = "5f0c1a3e-8d2b-4c1f-9a7e-2b6d4e8f1a03"
HANDLE = "conv-0001-invented"
MAIN = "https://main.example.test"


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class MemoryStore:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


class FakeSfuWs:
    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, data):
        if self.closed:
            raise ConnectionError("closed")

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


class QuietProviderSession:
    """A provider session that issues a resume handle and then says nothing until it is closed."""

    resumed = False

    def __init__(self):
        self._closed = asyncio.Event()
        self.audio_frames = 0

    async def open(self, instructions, reasoning_effort, tools, **kwargs):
        pass

    async def send_audio(self, frame):
        self.audio_frames += 1

    async def receive_events(self):
        yield RealtimeSessionEvent(type="resume_handle", payload={"handle": HANDLE})
        await self._closed.wait()

    async def submit_tool_result(self, call_id, output):
        pass

    async def submit_message(self, role, text):
        pass

    async def request_response(self):
        pass

    async def cancel_response(self):
        pass

    async def truncate(self, item_id, audio_end_ms):
        pass

    async def close(self):
        self._closed.set()


def _mic_packet(seq: int) -> bytes:
    tone = (np.sin(np.arange(960) / 3) * 8000).astype("<i2")
    return encode_sfu_packet(seq, seq * 960, np.repeat(tone, 2).astype("<i2").tobytes())


def _dead_peer_lines(caplog):
    return [r.getMessage() for r in caplog.records if "dead peer" in r.getMessage()]


class EndPathWorld:
    """Relay and main service on one store: the ticket record and the one-call marker as a live interpreter call left
    them before the relay redeemed the ticket."""

    def __init__(self, clock: Clock, timeout_s: float) -> None:
        self.store = MemoryStore({
            ticket_key(T1): {"instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1",
                             "call_kind": "web", "mode": "interpreter", "tools": [],
                             "voice": {"provider": "xai", "turn_ownership": "provider", "voice": "rex",
                                       "reasoning_effort": "low"}},
            call_kind_key(T1): {"call_kind": "web", "mode": "interpreter"},
            "voice_one_call:usr-1": {"in_flight": True, "call_id": "web-call-0001"},
        })
        main = Quart("main_service")
        main.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
            summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
            digest_key=DIGEST_KEY, resume_service=VoiceResumeService(self.store)))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main), base_url=MAIN)
        self.provider = QuietProviderSession()
        service = VoiceSessionService(
            realtime_session_factory=lambda spec: self.provider,
            control_plane=HttpCallControlPlaneAdapter(MAIN, id_token_provider=lambda: "relay-token", http_client=self.http),
            alert_sink=AsyncMock())
        self.handler = SfuStreamHandler(service, clock=clock, gap_poll_s=0.01, frame_interval_s=0.001,
                                        dead_peer_timeout_s=timeout_s)


@pytest.mark.asyncio
async def test_a_call_idle_for_n_seconds_ends_through_the_end_path(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    world = EndPathWorld(clock, timeout_s=60.0)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(world.handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(world.handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await egress.incoming.put(_mic_packet(1))
    for _ in range(100):  # the call is live: its ticket redeemed, the packet forwarded to the provider
        if world.provider.audio_frames:
            break
        await asyncio.sleep(0.01)
    assert world.provider.audio_frames == 1 and ticket_key(T1) not in world.store.data

    clock.now = 59.0
    await asyncio.sleep(0.05)
    assert not _dead_peer_lines(caplog) and "voice_one_call:usr-1" in world.store.data

    clock.now = 60.0
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

    # The main side got the report: the marker is released and the call stays resumable (not a button hangup).
    assert "voice_one_call:usr-1" not in world.store.data
    assert world.store.data["voice_resume:usr-1"] == {"handle": HANDLE, "mode": "interpreter", "provider": "xai"}
    assert ingest.closed and egress.closed
    lines = _dead_peer_lines(caplog)
    assert lines == [lines[0]] and "dead peer: no inbound audio for 60s, ending call" in lines[0]
    assert T1 not in lines[0]
    await world.http.aclose()


@pytest.mark.asyncio
async def test_a_packet_resets_the_timer(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    handler = SfuStreamHandler(session_service=None, clock=clock, gap_poll_s=0.01, dead_peer_timeout_s=10.0)
    call = _SfuCall(ticket=T1)
    watch = asyncio.ensure_future(handler._watch_gaps(call))
    await asyncio.sleep(0.03)
    clock.now = 8.0
    handler._note_packet(call)
    clock.now = 17.0  # 17 s after arming, 9 s after the packet
    await asyncio.sleep(0.05)
    assert not call.ended.is_set() and not _dead_peer_lines(caplog)

    clock.now = 18.0  # 10 s after the packet
    await asyncio.wait_for(watch, timeout=1)
    assert call.ended.is_set() and len(_dead_peer_lines(caplog)) == 1


class _CountingService:
    def __init__(self):
        self.started = 0

    async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio, playback):
        self.started += 1
        async for _ in inbound_audio:
            pass


@pytest.mark.asyncio
async def test_the_watchdog_is_not_armed_before_pairing(caplog):
    caplog.set_level(logging.INFO)
    clock = Clock()
    service = _CountingService()
    handler = SfuStreamHandler(service, clock=clock, gap_poll_s=0.01, frame_interval_s=0.001,
                               egress_reattach_s=0.05, pair_timeout_s=5.0, dead_peer_timeout_s=60.0)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    t_eg = asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))
    await egress.incoming.put(_mic_packet(1))
    await asyncio.sleep(0.03)
    clock.now = 1000.0  # far past N while only one socket is there, the last packet long ago
    await asyncio.sleep(0.03)
    t_in = asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}"))
    await asyncio.sleep(0.1)

    # Paired now: the call runs, and N counts from pairing, not from the packet before it.
    call = handler._calls[T1]
    assert service.started == 1 and not call.ended.is_set() and not _dead_peer_lines(caplog)

    clock.now = 1060.0
    await asyncio.wait_for(asyncio.gather(t_eg, t_in), timeout=2)
    assert len(_dead_peer_lines(caplog)) == 1


async def _ends_at(handler, clock, before: float, at: float) -> bool:
    call = _SfuCall(ticket=T1)
    watch = asyncio.ensure_future(handler._watch_gaps(call))
    await asyncio.sleep(0.03)
    clock.now = before
    await asyncio.sleep(0.05)
    still_running = not call.ended.is_set()
    clock.now = at
    await asyncio.wait_for(watch, timeout=1)
    return still_running and call.ended.is_set()


@pytest.mark.asyncio
async def test_the_knob_overrides_n(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("VOICE_DEAD_PEER_TIMEOUT_S", "30")
    clock = Clock()
    handler = SfuStreamHandler(session_service=None, clock=clock, gap_poll_s=0.01,
                               dead_peer_timeout_s=relay_main._dead_peer_timeout_s())

    assert await _ends_at(handler, clock, before=29.0, at=30.0)
    assert "no inbound audio for 30s" in _dead_peer_lines(caplog)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, "", "soon", "0", "-5"])
async def test_without_a_usable_knob_n_is_ten_minutes(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("VOICE_DEAD_PEER_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("VOICE_DEAD_PEER_TIMEOUT_S", raw)
    clock = Clock()
    handler = SfuStreamHandler(session_service=None, clock=clock, gap_poll_s=0.01,
                               dead_peer_timeout_s=relay_main._dead_peer_timeout_s())

    assert await _ends_at(handler, clock, before=599.0, at=600.0)
