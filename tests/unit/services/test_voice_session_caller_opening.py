"""The call-opening note the main service puts in the session config opens a fresh call as a user item, in place of
the pickup note: live, a user item steered Lelik where system text did not. Without it (interpreter, tutor, or a web
call with the VOICE_WEB_CALLER_OPENING lever off) the pickup note opens the call; a resumed call gets neither."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService


# The call-opening note the main service puts in the session config (decisions/lelik_call_opening.md).
_NOTE = "<system_note key=\"able-acorn-absorb\">\nthe call has just connected\n</system_note>"
_PROVIDER = {"provider": "xai", "turn_ownership": "provider", "voice": "rex", "reasoning_effort": "low"}
_PICKUP = "[The caller has just picked up the phone you called. Speak first.]"


async def _open_call(extra_config, resumed=False, **service_kwargs):
    session = AsyncMock()
    session.resumed = resumed

    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": [], "voice": _PROVIDER,
        **extra_config}

    async def inbound():
        await asyncio.sleep(0.2)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(), **service_kwargs)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return [c.args for c in session.submit_message.await_args_list], session


@pytest.mark.asyncio
async def test_a_fresh_call_with_the_config_opening_sends_it_as_a_user_item_and_no_pickup_note():
    messages, session = await _open_call({"opening": _NOTE})

    assert messages == [("user", _NOTE)]
    session.request_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_fresh_call_without_the_config_opening_sends_the_pickup_note():
    messages, _ = await _open_call({})

    assert messages == [("system", _PICKUP)]


@pytest.mark.asyncio
async def test_a_resumed_call_sends_neither_the_opening_nor_the_pickup_note():
    messages, _ = await _open_call({"opening": _NOTE, "resume": {"handle": "h-1"}}, resumed=True)

    assert len(messages) == 1
    assert messages[0][0] == "system" and "dropped" in messages[0][1]


@pytest.mark.asyncio
async def test_a_web_call_uses_the_opening_while_the_knob_is_on_and_the_pickup_note_when_it_is_off():
    on, _ = await _open_call({"opening": _NOTE, "call_kind": "web"})
    off, _ = await _open_call({"opening": _NOTE, "call_kind": "web"}, web_opening_enabled=False)

    assert on == [("user", _NOTE)]
    assert [role for role, _ in off] == ["system"] and "web page" in off[0][1]


@pytest.mark.asyncio
async def test_a_phone_call_uses_the_opening_even_with_the_web_knob_off():
    messages, _ = await _open_call({"opening": _NOTE, "call_kind": "phone"}, web_opening_enabled=False)

    assert messages == [("user", _NOTE)]
