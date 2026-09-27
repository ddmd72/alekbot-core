import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_module
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]


def _tool_call(call_id="c1"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": "search_web", "query": "weather"})})


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


async def _hold():
    await asyncio.sleep(10)


async def _call(events, delegate, seconds=0.3, barge_in_min_speech_s=0.0, silence_timeout_s=8.0):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    control.delegate.side_effect = delegate

    async def inbound():
        await asyncio.sleep(seconds)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  barge_in_min_speech_s=barge_in_min_speech_s,
                                  silence_timeout_s=silence_timeout_s)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control


def _messages(session):
    return [c.args[1] for c in session.submit_message.await_args_list]


async def _never(**_):
    await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_dispatch_filler_starts_immediately_after_the_dispatching_turn():
    async def events():
        for e in _opening():
            yield e
        yield E(type="response_created", payload={})
        yield _tool_call()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, _never)
    notes = _messages(session)
    assert any(n == voice_module._DISPATCH_NOTE for n in notes)
    # opening line + the immediate dispatch filler; the watchdog's own note is 8 s away
    assert session.request_response.await_count == 2


@pytest.mark.asyncio
async def test_no_filler_when_the_answer_already_arrived_and_was_flushed():
    async def quick(**_):
        await asyncio.sleep(0.01)
        return "sunny"

    async def events():
        for e in _opening():
            yield e
        yield E(type="response_created", payload={})
        yield _tool_call()
        await asyncio.sleep(0.05)  # the answer resolves and queues behind the still-active response
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, quick)
    notes = _messages(session)
    assert not any(n == voice_module._DISPATCH_NOTE for n in notes)
    session.submit_tool_result.assert_awaited_once_with("c1", "sunny")


@pytest.mark.asyncio
async def test_no_filler_when_the_dispatching_response_was_barged_into():
    async def events():
        for e in _opening():
            yield e
        yield E(type="response_created", payload={})
        yield _tool_call()
        yield E(type="speech_started", payload={})
        yield E(type="speech_stopped", payload={})
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    session, _ = await _call(events, _never)
    notes = _messages(session)
    assert not any(n == voice_module._DISPATCH_NOTE for n in notes)


@pytest.mark.asyncio
async def test_no_filler_when_the_caller_is_speaking_at_response_done():
    async def events():
        for e in _opening():
            yield e
        yield E(type="response_created", payload={})
        yield _tool_call()
        yield E(type="speech_started", payload={})
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    # barge_in_min_speech_s > 0 so speech_started only arms a pending confirmation instead of
    # cancelling the response outright - isolates "caller still speaking" from "was cancelled".
    session, _ = await _call(events, _never, barge_in_min_speech_s=5.0)
    notes = _messages(session)
    assert not any(n == voice_module._DISPATCH_NOTE for n in notes)
