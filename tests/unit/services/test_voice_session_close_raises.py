"""The call-end report survives a failing provider close (LELIK_VOICE_MCP_RFC §5.3: the report deletes the call's
voice MCP live-call record, so skipping it would leave the call's tokens alive to their exp)."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.services.voice_session_service import VoiceSessionService


class RecordingControlPlane:
    """The relay's control-plane boundary: serves one session config, records each call-end report."""

    def __init__(self) -> None:
        self.reports: list = []

    async def fetch_session_config(self, ticket: str) -> dict:
        return {"instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1"}

    async def submit_transcript(self, **kwargs) -> None:
        self.reports.append(kwargs)


async def _no_frames():
    return
    yield  # pragma: no cover


async def test_a_close_that_raises_still_reports_the_call_end_and_is_logged_then_raised(caplog):
    control_plane = RecordingControlPlane()
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_done", payload={"usage": {}, "model": "m"})

    realtime_session.receive_events = MagicMock(return_value=events())
    realtime_session.close.side_effect = RuntimeError("socket already gone")
    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=realtime_session),
                                  control_plane=control_plane, alert_sink=AsyncMock())
    caplog.set_level(logging.ERROR)

    with pytest.raises(RuntimeError, match="socket already gone"):
        await service.handle_call(ticket="ticket-0001-invented", inbound_audio=_no_frames(),
                                  send_outbound_audio=AsyncMock(), clear_outbound_audio=AsyncMock(),
                                  playback=PlaybackTracker())

    assert [(r["call_id"], r["user_id"], r["account_id"]) for r in control_plane.reports] == [
        ("ticket-0001-invented", "usr-1", "acc-1")]
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("closing the realtime session failed: RuntimeError" in m for m in errors)
    assert not any("ticket-0001-invented" in m for m in errors)
