"""On a provider-owned fresh call the persona anchor is submitted once as a system item, before the caller opening and
the pickup note; relay-owned calls (anchor on every response) and resumed calls get no start item."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.llm import build_persona_anchor
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

PERSONA = "identity {\n  x\n}\nhumor_engine {\n  y\n}\nspoken_delivery {\n  z\n}"
_PROVIDER = {"provider": "xai", "turn_ownership": "provider", "voice": "rex", "reasoning_effort": "low"}
_RELAY = {**_PROVIDER, "turn_ownership": "relay"}


async def _call(instructions, voice=_PROVIDER, resumed=False, resume=False, caller_opening=None):
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
        "call_kind": "web", "voice": voice, **({"resume": {"handle": "h-1"}} if resume else {})}

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
async def test_provider_owned_fresh_call_submits_the_anchor_before_the_pickup_note():
    anchor = build_persona_anchor(PERSONA)
    assert anchor is not None

    submitted = await _call(PERSONA)

    assert len(submitted) == 2
    assert submitted[0] == ("system", anchor)
    assert "connected to you from the web page" in submitted[1][1]


@pytest.mark.asyncio
async def test_provider_owned_instructions_without_persona_sections_get_no_start_item():
    submitted = await _call("you are a helper")

    assert len(submitted) == 1 and "connected to you from the web page" in submitted[0][1]


@pytest.mark.asyncio
async def test_relay_owned_call_gets_no_start_item():
    anchor = build_persona_anchor(PERSONA)

    submitted = await _call(PERSONA, voice=_RELAY)

    # The relay's own per-response anchor follows the pickup note; nothing is placed before it.
    assert "connected to you from the web page" in submitted[0][1]
    assert submitted.count(("system", anchor)) <= 1 and submitted[0] != ("system", anchor)


@pytest.mark.asyncio
async def test_resumed_provider_owned_call_gets_no_start_item():
    anchor = build_persona_anchor(PERSONA)

    submitted = await _call(PERSONA, resumed=True, resume=True)

    assert all(text != anchor for _, text in submitted)
    assert len(submitted) == 1 and "dropped" in submitted[0][1]
