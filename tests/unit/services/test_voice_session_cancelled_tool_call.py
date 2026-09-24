"""A tool call arriving for a response already cancelled by barge-in is answered, not run
(VOICE_COMPANION_RFC — live incident 2026-09-24 09:29:09, M4)."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]


def _tool_call(call_id="c1", intent="search_web", query="weather"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": intent, "query": query})})


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


async def _hold():
    await asyncio.sleep(10)


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
    return session, control


@pytest.mark.asyncio
async def test_tool_call_from_a_cancelled_response_is_answered_not_dispatched():
    async def events():
        # The opening reply's own response lifecycle: barged into before it finished, and the
        # tool call arrives while it is already cancelled (live, 2026-09-24 09:29:09).
        yield E(type="response_created", payload={})
        yield E(type="speech_started", payload={})
        yield _tool_call()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, AsyncMock(return_value="sunny"))

    control.delegate.assert_not_awaited()
    session.submit_tool_result.assert_awaited_once_with(
        "c1", "Not run: the caller interrupted before this request was complete.")
    # Only the opening request_response — nothing dispatched, so nothing later replies to it.
    assert session.request_response.await_count == 1


@pytest.mark.asyncio
async def test_tool_call_without_a_prior_cancel_is_dispatched_as_before():
    async def events():
        for e in _opening():
            yield e
        yield E(type="response_created", payload={})
        yield _tool_call()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, control = await _call(events, AsyncMock(return_value="sunny"))

    control.delegate.assert_awaited_once()
    session.submit_tool_result.assert_awaited_once_with("c1", "sunny")
