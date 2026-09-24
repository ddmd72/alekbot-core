"""A late-arriving delegation TIMEOUT must not read as an answer that "just arrived" (M8)."""
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


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


async def _hold():
    await asyncio.sleep(10)


async def _call(events, delegate, timeout_s=90.0):
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
                                  control_plane=control, alert_sink=AsyncMock(),
                                  delegation_timeout_s=timeout_s)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control


@pytest.mark.asyncio
async def test_late_timeout_reads_as_no_answer_not_as_one_that_arrived():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        yield E(type="speech_started", payload={})  # caller speaks: the eventual output is "late"
        yield E(type="speech_stopped", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, never, timeout_s=0.05)

    notes = [c.args[1] for c in session.submit_message.await_args_list if "no answer in time" in c.args[1]]
    assert len(notes) == 1
    assert "search_web: amazon whey protein links" in notes[0]
    assert "just arrived" not in notes[0]


@pytest.mark.asyncio
async def test_late_answer_that_is_not_a_timeout_still_says_just_arrived():
    async def slow(**_):
        await asyncio.sleep(0.05)
        return "sunny"

    async def events():
        for e in _opening():
            yield e
        yield _tool_call()
        yield E(type="speech_started", payload={})
        yield E(type="speech_stopped", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, slow)

    notes = [c.args[1] for c in session.submit_message.await_args_list if "just arrived" in c.args[1]]
    assert len(notes) == 1
    assert "sunny" in notes[0]
