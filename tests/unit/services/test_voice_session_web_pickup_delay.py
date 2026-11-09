"""A web call's pickup waits for the browser to subscribe to Lelik's audio; a phone call's never does."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services import voice_session_service as vss
from src.services.voice_session_service import VoiceSessionService

_REAL_SLEEP = asyncio.sleep


class _Harness:
    """One call whose pickup `request_response` and the delay's sleep are observable."""

    def __init__(self, monkeypatch, call_kind, delay, early_events=()):
        self.release = asyncio.Event()
        self.sleeps = []
        self.requests = 0

        async def fake_sleep(seconds, *args, **kwargs):
            if delay > 0 and seconds == delay:
                self.sleeps.append(seconds)
                await self.release.wait()
                return
            await _REAL_SLEEP(seconds, *args, **kwargs)

        monkeypatch.setattr(vss.asyncio, "sleep", fake_sleep)
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
    assert h.sleeps == [1.2] and h.requests == 0
    h.release.set()
    await h.settle()
    assert h.requests == 1
    await h.stop()


@pytest.mark.asyncio
async def test_phone_call_with_the_same_service_picks_up_immediately(monkeypatch):
    h = _Harness(monkeypatch, None, 1.2)
    await h.start()
    assert h.sleeps == [] and h.requests == 1
    await h.stop()


@pytest.mark.asyncio
async def test_default_delay_is_zero_and_pickup_is_immediate(monkeypatch):
    h = _Harness(monkeypatch, "web", 0.0)
    await h.start()
    assert h.sleeps == [] and h.requests == 1
    await h.stop()


@pytest.mark.asyncio
async def test_response_started_by_the_provider_during_the_wait_suppresses_the_pickup(monkeypatch):
    h = _Harness(monkeypatch, "web", 1.2, early_events=[E(type="response_created", payload={})])
    await h.start()
    assert h.requests == 0
    h.release.set()
    await h.settle()
    assert h.requests == 0
    await h.stop()
