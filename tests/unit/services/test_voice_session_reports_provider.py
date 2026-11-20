"""The call-end report names the provider the session ran on, so the main side can record it with each turn."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.services.voice_session_service import VoiceSessionService


class RecordingControlPlane:
    def __init__(self) -> None:
        self.reports: list = []

    async def fetch_session_config(self, ticket: str) -> dict:
        return {"instructions": "you are Lelik", "user_id": "u1", "account_id": "acc-1",
                "voice": {"provider": "xai", "turn_ownership": "provider", "voice": "v1", "reasoning_effort": "low"}}

    async def submit_transcript(self, **kwargs) -> None:
        self.reports.append(kwargs)


async def _no_frames():
    return
    yield  # pragma: no cover


@pytest.mark.asyncio
async def test_the_call_end_report_names_the_session_provider():
    control_plane = RecordingControlPlane()
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_done", payload={"usage": {}, "model": "m"})

    realtime_session.receive_events = MagicMock(return_value=events())
    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=realtime_session),
                                  control_plane=control_plane, alert_sink=AsyncMock())

    await service.handle_call(ticket="t1", inbound_audio=_no_frames(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())

    assert [r["provider"] for r in control_plane.reports] == ["xai"]
