"""tell_alek errands in the relay (VOICE_COMPANION_RFC §4.15.2).

Only an acknowledgement comes back, in well under a second. UAT 2026-09-29: the dispatch filler
fired for it and Lelik said "handed to Alek" twice. An errand is not waited for: no filler, and
the acknowledgement is still handed to the model as the tool call's output.
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
_ACK = "Task started in background. You will be notified when complete."


def _tool_call(intent, call_id="c1"):
    return E(type="tool_call", payload={"call_id": call_id, "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": intent, "query": "remind me at 10"})})


async def _call(intent, delegate, seconds=0.4):
    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        yield E(type="response_created", payload={})
        yield _tool_call(intent)
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

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
                                  control_plane=control, alert_sink=AsyncMock(), barge_in_min_speech_s=0.0)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control


async def _slow_ack(**_):
    # Slower than the turn's response_done, so a filler would have claimed the reply slot first.
    await asyncio.sleep(0.05)
    return _ACK


def _notes(session):
    return [c.args[1] for c in session.submit_message.await_args_list]


@pytest.mark.asyncio
async def test_an_errand_gets_no_dispatch_filler_but_its_ack_reaches_the_model():
    session, control = await _call("tell_alek", _slow_ack)

    assert voice_module._DISPATCH_NOTE not in _notes(session)
    session.submit_tool_result.assert_awaited_once_with("c1", _ACK)
    control.abandon_delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_question_still_gets_the_filler():
    session, _ = await _call("ask_alek", _slow_ack)

    assert voice_module._DISPATCH_NOTE in _notes(session)


@pytest.mark.asyncio
async def test_an_errand_still_in_flight_at_hang_up_is_not_abandoned():
    async def never(**_):
        await asyncio.sleep(10)

    _, control = await _call("tell_alek", never, seconds=0.2)

    control.abandon_delegation.assert_not_awaited()
