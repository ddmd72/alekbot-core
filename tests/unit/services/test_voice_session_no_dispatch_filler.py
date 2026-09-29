"""No dispatch filler after a delegating turn (VOICE_COMPANION_RFC §4.15, removed 2026-09-29).

The turn that dispatches already carries Lelik's acknowledgement ("Sent the scouts out."). The
filler that followed it made him add a second "it's on its way". A long wait is still covered
by the silence watchdog's _WAITING_NOTE.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_module
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]


@pytest.mark.asyncio
async def test_the_dispatching_turn_is_not_followed_by_another_reply():
    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        yield E(type="response_created", payload={})
        yield E(type="tool_call", payload={"call_id": "c1", "name": "delegate_to_specialist",
                                           "arguments": json.dumps({"intent": "ask_alek", "query": "q"})})
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    async def never(**_):
        await asyncio.sleep(10)

    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = never

    async def inbound():
        await asyncio.sleep(0.3)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(), barge_in_min_speech_s=0.0)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())

    assert session.request_response.await_count == 1  # the opening line only
    assert not hasattr(voice_module, "_DISPATCH_NOTE")
