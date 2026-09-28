"""The pickup note must not tell Lelik he dialed a phone when the caller pressed a button on a web page."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService


async def _pickup_note(config_extra):
    session = AsyncMock()

    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": [], **config_extra}

    async def inbound():
        await asyncio.sleep(0.2)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock())
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return [text for role, text in (c.args for c in session.submit_message.await_args_list) if role == "system"][0]


@pytest.mark.asyncio
async def test_web_call_pickup_note_says_connected_not_phone():
    note = await _pickup_note({"call_kind": "web"})
    assert "phone" not in note and "Speak first" in note


@pytest.mark.asyncio
async def test_missing_call_kind_keeps_the_phone_note():
    assert "picked up the phone" in await _pickup_note({})
