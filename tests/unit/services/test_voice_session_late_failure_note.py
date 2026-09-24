"""A late-arriving delegation FAILURE must not read as an answer that "just arrived"."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]


def _tool_call(call_id="c1", intent="search_web", query="amazon whey protein links"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": intent, "query": query})})


async def _call(events, delegate):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = delegate

    async def inbound():
        await asyncio.sleep(0.3)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock())
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session


@pytest.mark.asyncio
async def test_late_failure_reads_as_did_not_go_through_not_as_one_that_arrived():
    async def broken(**_):
        await asyncio.sleep(0.05)
        raise RuntimeError("control plane 500")

    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        yield _tool_call()
        yield E(type="speech_started", payload={})  # caller speaks: the eventual output is "late"
        yield E(type="speech_stopped", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session = await _call(events, broken)

    notes = [c.args[1] for c in session.submit_message.await_args_list if "did not go through" in c.args[1]]
    assert len(notes) == 1
    assert "search_web: amazon whey protein links" in notes[0]
    assert "just arrived" not in notes[0]
