"""A web call's greeting waits for the page's `listening` on its control link, at most `web_pickup_max_wait_s`
(VOICE_CALL_CONTROL_CHANNEL_RFC §4.4): right after the signal, never before it; on the cap without one; the whole cap
without a link; never on a phone call. A provider-owned reply during the wait still suppresses the pickup, and a call
that ends during the wait gets no greeting. The cap is driven by hand (the module's `asyncio.wait` with the cap as its
timeout waits on an event instead), so no test depends on the wall clock."""
import asyncio
import logging
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_playback_tracker import PlaybackTracker
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services import voice_session_service as vss
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import VoiceSessionService
from tests.support.control_socket_fake import FakeControlSocket

_REAL_WAIT = asyncio.wait
_REAL_SLEEP = asyncio.sleep
CAP = 6.0
TOKEN = "token-7c41e09a-invented"
TICKET = "ticket-5be2d3f8-invented"
DIGEST = control_token_digest(TOKEN)


class _Call:
    """One call whose pickup `request_response`, the cap's timer and the inbound stream's end are in the test's hands."""

    def __init__(self, monkeypatch, *, call_kind="web", digest=True, hub=True, cap=CAP, early_events=(),
                 open_gate=None):
        self.cap_fires = asyncio.Event()
        self.inbound_ends = asyncio.Event()
        self.cap_waits = []
        self.requests = 0
        self.hub = CallControlHub() if hub else None

        async def fake_wait(fs, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            if cap > 0 and timeout == cap:
                self.cap_waits.append(timeout)
                timer = asyncio.ensure_future(self.cap_fires.wait())
                try:
                    done, pending = await _REAL_WAIT(set(fs) | {timer}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    timer.cancel()
                return done - {timer}, pending - {timer}
            return await _REAL_WAIT(fs, timeout=timeout, return_when=return_when)

        monkeypatch.setattr(vss.asyncio, "wait", fake_wait)
        self.session = AsyncMock()

        async def request_response():
            self.requests += 1

        self.session.request_response.side_effect = request_response
        if open_gate is not None:
            async def slow_open(**kwargs):
                await open_gate.wait()

            self.session.open.side_effect = slow_open

        async def events():
            for event in early_events:
                yield event
            await _REAL_SLEEP(30)
            yield  # pragma: no cover

        self.session.receive_events = MagicMock(return_value=events())
        self.control_plane = AsyncMock()
        self.control_plane.fetch_session_config.return_value = {
            "instructions": "you are Lelik", "user_id": "usr-3f9a", "account_id": "acc-81d2", "tools": [],
            **({"call_kind": call_kind} if call_kind else {}),
            **({"control_token_sha256": DIGEST} if digest else {})}
        self.service = VoiceSessionService(
            realtime_session_factory=MagicMock(return_value=self.session), control_plane=self.control_plane,
            alert_sink=AsyncMock(), control_hub=self.hub, web_pickup_max_wait_s=cap)

    async def _inbound(self):
        await self.inbound_ends.wait()
        return
        yield  # pragma: no cover

    def link(self):
        return self.hub.get(DIGEST)

    async def start(self):
        self.task = asyncio.ensure_future(self.service.handle_call(
            ticket=TICKET, inbound_audio=self._inbound(), send_outbound_audio=AsyncMock(),
            clear_outbound_audio=AsyncMock(), playback=PlaybackTracker()))
        await self.settle()

    @staticmethod
    async def settle():
        for _ in range(30):
            await _REAL_SLEEP(0)

    async def stop(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def _pickup_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and ": pickup " in r.getMessage()]


@pytest.mark.asyncio
async def test_the_greeting_starts_right_after_the_page_listened_and_not_before(monkeypatch, caplog):
    call = _Call(monkeypatch)
    with caplog.at_level(logging.INFO):
        await call.start()
        assert call.cap_waits == [CAP] and call.requests == 0      # waiting on the signal
        call.link().mark_listening()
        await call.settle()
        assert call.requests == 1
        call.cap_fires.set()                                       # the cap passing later starts nothing more
        await call.settle()
        assert call.requests == 1
    lines = _pickup_lines(caplog)
    assert len(lines) == 1 and lines[0].startswith(f"voice call {fingerprint(TICKET)}: pickup after the page listened (")
    assert lines[0].endswith(" ms)") and TOKEN not in lines[0] and TICKET not in lines[0]
    await call.stop()


@pytest.mark.asyncio
async def test_a_signal_that_came_while_the_session_opened_starts_the_greeting_at_once(monkeypatch, caplog):
    gate = asyncio.Event()
    call = _Call(monkeypatch, open_gate=gate)
    with caplog.at_level(logging.INFO):
        await call.start()
        call.link().mark_listening()          # the link is open before the provider session is
        gate.set()
        await call.settle()
    assert call.requests == 1 and not call.cap_fires.is_set()   # no cap involved: it starts on the earlier signal
    lines = _pickup_lines(caplog)
    assert len(lines) == 1
    assert re.fullmatch(rf"voice call {fingerprint(TICKET)}: pickup after the page listened \(\d+ ms\)", lines[0])
    await call.stop()


@pytest.mark.asyncio
async def test_without_a_signal_the_greeting_starts_on_the_cap(monkeypatch, caplog):
    call = _Call(monkeypatch)
    with caplog.at_level(logging.INFO):
        await call.start()
        assert call.requests == 0
        call.cap_fires.set()
        await call.settle()
    assert call.requests == 1
    assert _pickup_lines(caplog) == [f"voice call {fingerprint(TICKET)}: pickup on the cap (6 s), no listening signal"]
    await call.stop()


@pytest.mark.parametrize("digest, hub", [(False, True), (True, False)], ids=["no-token", "no-hub"])
@pytest.mark.asyncio
async def test_a_web_call_without_a_control_link_waits_the_whole_cap(monkeypatch, caplog, digest, hub):
    call = _Call(monkeypatch, digest=digest, hub=hub)
    with caplog.at_level(logging.INFO):
        await call.start()
        assert call.cap_waits == [CAP] and call.requests == 0
        call.cap_fires.set()
        await call.settle()
    assert call.requests == 1
    assert _pickup_lines(caplog) == [f"voice call {fingerprint(TICKET)}: pickup on the cap (6 s), no listening signal"]
    await call.stop()


@pytest.mark.asyncio
async def test_a_phone_call_never_waits(monkeypatch, caplog):
    call = _Call(monkeypatch, call_kind=None)
    with caplog.at_level(logging.INFO):
        await call.start()
    assert call.cap_waits == [] and call.requests == 1 and _pickup_lines(caplog) == []
    await call.stop()


@pytest.mark.asyncio
async def test_no_cap_means_no_wait(monkeypatch):
    call = _Call(monkeypatch, cap=0.0)
    await call.start()
    assert call.cap_waits == [] and call.requests == 1
    await call.stop()


@pytest.mark.asyncio
async def test_a_provider_reply_that_started_during_the_wait_suppresses_the_pickup(monkeypatch):
    call = _Call(monkeypatch, early_events=[E(type="response_created", payload={})])
    await call.start()
    call.link().mark_listening()
    await call.settle()
    assert call.requests == 0
    await call.stop()


@pytest.mark.asyncio
async def test_a_call_that_ends_during_the_wait_gets_no_greeting_and_tears_down_at_once(monkeypatch, caplog):
    call = _Call(monkeypatch)
    with caplog.at_level(logging.INFO):
        await call.start()
        call.inbound_ends.set()                    # the browser's leg is gone before the page listened
        await asyncio.wait_for(call.task, timeout=2)
    assert call.requests == 0 and not call.cap_fires.is_set() and _pickup_lines(caplog) == []
    call.control_plane.submit_transcript.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_page_saying_listening_twice_on_the_socket_starts_one_greeting(monkeypatch):
    call = _Call(monkeypatch)
    await call.start()
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    socket_task = asyncio.ensure_future(CallControlHandler(call.hub, link_wait_s=1.0).handle_connection(page))
    await call.settle()
    assert page.types() == ["welcome"] and call.requests == 0
    page.page_says({"type": "listening"})
    page.page_says({"type": "listening"})
    await call.settle()
    assert call.requests == 1 and not page.closed
    page.page_leaves()
    await asyncio.wait_for(socket_task, timeout=2)
    await call.stop()
