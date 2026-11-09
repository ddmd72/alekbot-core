"""A web call's pickup waits for the browser to subscribe to Lelik's audio; a phone call's never does.

These calls have no control link (no hub), so a web call's wait is the whole `web_pickup_max_wait_s` cap
(VOICE_CALL_CONTROL_CHANNEL_RFC §4.4); the waits on the page's `listening` are in
`test_voice_session_pickup_on_listening.py`. The cap is the module's `asyncio.wait` with the cap as its timeout; the
harness ends it by hand (`release`), so no test depends on the wall clock."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services import voice_session_service as vss
from src.services.voice_session_service import VoiceSessionService

_REAL_SLEEP = asyncio.sleep
_REAL_WAIT = asyncio.wait


class _Harness:
    """One call whose pickup `request_response` and the cap's wait are observable; `release` is the cap passing."""

    def __init__(self, monkeypatch, call_kind, delay, early_events=()):
        self.release = asyncio.Event()
        self.waits = []
        self.requests = 0

        async def fake_wait(fs, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            if delay > 0 and timeout == delay:
                self.waits.append(timeout)
                timer = asyncio.ensure_future(self.release.wait())
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

        async def events():
            for event in early_events:
                yield event
            await _REAL_SLEEP(30)
            yield  # pragma: no cover

        self.session.receive_events = MagicMock(return_value=events())
        control = AsyncMock()
        control.fetch_session_config.return_value = {
            "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": [],
            **({"call_kind": call_kind} if call_kind else {})}
        self.service = VoiceSessionService(
            realtime_session_factory=MagicMock(return_value=self.session), control_plane=control,
            alert_sink=AsyncMock(), web_pickup_max_wait_s=delay)

    @staticmethod
    async def _inbound():
        await _REAL_SLEEP(30)
        return
        yield  # pragma: no cover

    async def start(self):
        self.task = asyncio.ensure_future(self.service.handle_call(
            ticket="t1", inbound_audio=self._inbound(), send_outbound_audio=AsyncMock(),
            clear_outbound_audio=AsyncMock(), playback=PlaybackTracker()))
        await self.settle()

    @staticmethod
    async def settle():
        for _ in range(30):
            await _REAL_SLEEP(0)

    async def stop(self):
        self.release.set()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_call_waits_then_requests_one_pickup_response(monkeypatch):
    h = _Harness(monkeypatch, "web", 1.2)
    await h.start()
    assert h.waits == [1.2] and h.requests == 0
    h.release.set()
    await h.settle()
    assert h.requests == 1
    await h.stop()


@pytest.mark.asyncio
async def test_phone_call_with_the_same_service_picks_up_immediately(monkeypatch):
    h = _Harness(monkeypatch, None, 1.2)
    await h.start()
    assert h.waits == [] and h.requests == 1
    await h.stop()


@pytest.mark.asyncio
async def test_default_delay_is_zero_and_pickup_is_immediate(monkeypatch):
    h = _Harness(monkeypatch, "web", 0.0)
    await h.start()
    assert h.waits == [] and h.requests == 1
    await h.stop()


@pytest.mark.asyncio
async def test_response_started_by_the_provider_during_the_wait_suppresses_the_pickup(monkeypatch):
    h = _Harness(monkeypatch, "web", 1.2, early_events=[E(type="response_created", payload={})])
    await h.start()
    assert h.waits == [1.2] and h.requests == 0
    h.release.set()
    await h.settle()
    assert h.requests == 0
    await h.stop()
