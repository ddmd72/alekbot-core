"""After the one "still there?" check, continued silence ends the call instead of holding a
voicemail box (or a phone put down) open until the carrier cuts it."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService


class _EchoSession:
    """Every requested response is created and finished at once, with no audio."""

    def __init__(self):
        self._events: asyncio.Queue = asyncio.Queue()
        self.notes = []

    async def open(self, **_):
        pass

    async def submit_message(self, role, text):
        self.notes.append(text)

    async def request_response(self):
        await self._events.put(E(type="response_created", payload={}))
        await self._events.put(E(type="response_done", payload={}))

    async def receive_events(self):
        while True:
            yield await self._events.get()

    async def send_audio(self, frame):
        pass

    async def close(self):
        pass


async def _call(hangup_after_s):
    session = _EchoSession()
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": []}

    async def inbound():
        await asyncio.sleep(5)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  silence_timeout_s=0.3, hangup_after_silence_s=hangup_after_s)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control, loop.time() - started


@pytest.mark.asyncio
async def test_silence_after_the_check_ends_the_call_and_still_submits_the_transcript():
    session, control, elapsed = await _call(hangup_after_s=0.5)

    assert elapsed < 3  # well before the inbound stream would have ended (5 s)
    assert sum("silent" in note for note in session.notes) == 1  # the one "still there?" check first
    control.submit_transcript.assert_awaited_once()


@pytest.mark.asyncio
async def test_without_a_hangup_setting_the_call_waits_for_the_line():
    _, _, elapsed = await _call(hangup_after_s=None)

    assert elapsed >= 4.5
