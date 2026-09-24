"""The caller's standing request opens the conversation as the caller's own turn: live, a
delivery request in the caller's words changed how Lelik spoke where system text did not."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService


async def _call(caller_opening):
    session = AsyncMock()

    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": []}

    async def inbound():
        await asyncio.sleep(0.2)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  caller_opening=caller_opening)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session


@pytest.mark.asyncio
async def test_caller_opening_is_the_first_item_as_a_user_turn_before_the_pickup_reply():
    session = await _call("Talk faster, greet me by name.")

    messages = [c.args for c in session.submit_message.await_args_list]
    assert messages[0] == ("user", "Talk faster, greet me by name.")
    assert messages[1][0] == "system" and "picked up" in messages[1][1]
    assert session.request_response.await_count == 1  # one reply: the greeting answers it


@pytest.mark.asyncio
async def test_no_caller_opening_means_no_user_item():
    session = await _call(None)

    assert all(role != "user" for role, _ in (c.args for c in session.submit_message.await_args_list))
