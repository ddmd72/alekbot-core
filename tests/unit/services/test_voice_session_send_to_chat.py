"""The relay and Lelik's send_to_chat tool (VOICE_COMPANION_RFC §4.15.2, revised 2026-10-29).

A `send_to_chat(request)` call is forwarded to the main service as the errand intent of the same name, and the relay
treats it as an errand: not waited for, never abandoned at hang-up, no waiting note. An `ask_alek` delegation is
forwarded unchanged and stays a delegation the relay waits for (abandoned at hang-up, so its answer goes to chat).
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_module
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}},
          {"name": "send_to_chat", "description": "c", "parameters": {"type": "object"}}]
_ACK = "Task started in background. You will be notified when complete."


class _ControlPlane:
    """Records what reached the main service; `answer` decides how each delegation ends."""

    def __init__(self, answer):
        self._answer = answer
        self.delegated = []
        self.abandoned = []

    async def fetch_session_config(self, ticket):
        return {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}

    async def delegate(self, **kwargs):
        self.delegated.append(kwargs)
        return await self._answer()

    async def abandon_delegation(self, ticket, call_id):
        self.abandoned.append(call_id)

    def __getattr__(self, name):  # submit_transcript and the like: not what these tests are about
        return AsyncMock()


def _tool_call(name, arguments):
    return E(type="tool_call", payload={"call_id": "c1", "name": name, "arguments": json.dumps(arguments)})


async def _call(tool_call, answer, seconds=0.2):
    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        yield E(type="response_created", payload={})
        yield tool_call
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = _ControlPlane(answer)

    async def inbound():
        await asyncio.sleep(seconds)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(), barge_in_min_speech_s=0.0)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control


async def _ack():
    await asyncio.sleep(0.05)
    return _ACK


async def _never():
    await asyncio.sleep(10)


def _notes(session):
    return [c.args[1] for c in session.submit_message.await_args_list]


@pytest.mark.asyncio
async def test_send_to_chat_reaches_the_main_service_as_the_errand_intent():
    _, control = await _call(_tool_call("send_to_chat", {"request": "the garage rules for guests, as a file"}), _ack)

    [delegated] = control.delegated
    assert delegated["arguments"] == {"intent": "send_to_chat", "query": "the garage rules for guests, as a file"}
    assert delegated["request"].startswith("send_to_chat: the garage rules")


@pytest.mark.asyncio
async def test_send_to_chat_hands_back_only_the_ack_and_says_nothing_else():
    session, control = await _call(_tool_call("send_to_chat", {"request": "the garage rules"}), _ack)

    session.submit_tool_result.assert_awaited_once_with("c1", _ACK)
    assert control.abandoned == []
    assert voice_module._WAITING_NOTE not in _notes(session)


@pytest.mark.asyncio
async def test_send_to_chat_still_in_flight_at_hang_up_is_not_abandoned():
    _, control = await _call(_tool_call("send_to_chat", {"request": "the garage rules"}), _never, seconds=0.2)

    assert len(control.delegated) == 1
    assert control.abandoned == []


@pytest.mark.asyncio
async def test_ask_alek_is_forwarded_unchanged_and_abandoned_at_hang_up():
    arguments = {"intent": "ask_alek", "query": "what do the garage rules say about guests?"}

    _, control = await _call(_tool_call("delegate_to_specialist", arguments), _never, seconds=0.2)

    [delegated] = control.delegated
    assert delegated["arguments"] == arguments
    assert control.abandoned == ["c1"]
