"""A fresh voice call opens with the language note, before the caller opening and the pickup note; a resumed call does
not get it (the earlier context holds it)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_language_note import build_language_note
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

RULED = "policies {\n  rule Output_Language_Fixed_EN (\n    speak English\n  )\n}"
_VOICE = {"provider": "xai", "turn_ownership": "relay", "voice": "rex", "reasoning_effort": "low"}


async def _call(instructions, resumed=False, resume=False, caller_opening=None):
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
        "instructions": instructions, "user_id": "usr-1", "account_id": "acc-1", "tools": [],
        "call_kind": "web", "voice": _VOICE, **({"resume": {"handle": "h-1"}} if resume else {})}

    async def inbound():
        await asyncio.sleep(0.2)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session), control_plane=control,
                                  alert_sink=AsyncMock(), caller_opening=caller_opening)
    await service.handle_call(ticket="ticket-0001-invented", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return [c.args for c in session.submit_message.await_args_list]


@pytest.mark.asyncio
async def test_a_fresh_call_submits_the_note_before_the_opening_and_the_pickup_note():
    submitted = await _call(RULED, caller_opening="Hello, it is me.")

    assert submitted[0] == ("system", build_language_note(RULED))
    assert submitted[1] == ("user", "Hello, it is me.")
    assert "connected to you from the web page" in submitted[2][1]


@pytest.mark.asyncio
async def test_a_fresh_call_without_opening_has_the_note_then_the_pickup_note():
    submitted = await _call(RULED)

    assert [role for role, _ in submitted] == ["system", "system"]
    assert submitted[0][1] == build_language_note(RULED)


@pytest.mark.asyncio
async def test_instructions_without_a_rule_get_no_note():
    submitted = await _call("you are a helper")

    assert len(submitted) == 1 and "connected to you from the web page" in submitted[0][1]


@pytest.mark.asyncio
async def test_a_resumed_call_gets_no_note():
    submitted = await _call(RULED, resumed=True, resume=True)

    assert len(submitted) == 1 and "dropped" in submitted[0][1]
    assert all("output-language rule" not in text for _, text in submitted)
