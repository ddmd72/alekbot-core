"""The session config's voice spec decides whether the relay's xAI session offers xAI's own search.

Main service: LelikAgent.session_config puts the spec (builtin_search on or absent) on the ticket. Relay:
VoiceSessionSpec.from_dict -> relay_main's per-call factory -> XaiRealtimeAdapter.open -> session.update.
"""
import json
from unittest.mock import AsyncMock

import pytest

import relay_main
from src.domain.voice_audio_format import MULAW_8K
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_session_spec import VoiceSessionSpec

_KEYS = {"openai": "sk-o", "xai": "xai-k"}
_DELEGATE = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}


class _Socket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


async def _session_tools(voice: dict) -> list:
    adapter = relay_main._realtime_session_factory(_KEYS, MULAW_8K)(VoiceSessionSpec.from_dict(voice))
    socket = _Socket()
    adapter._connect = AsyncMock(return_value=socket)
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[_DELEGATE])
    return socket.sent[0]["session"]["tools"]


@pytest.mark.asyncio
async def test_switched_on_the_xai_session_offers_web_and_x_search():
    voice = {**VOICE_PROVIDER_PROFILES["xai"].session.to_dict(), "builtin_search": True}

    tools = await _session_tools(voice)

    assert tools[:2] == [{"type": "web_search"}, {"type": "x_search"}]
    assert tools[2:] == [{"type": "function", **_DELEGATE}]


@pytest.mark.asyncio
async def test_switched_off_the_xai_session_is_unchanged():
    tools = await _session_tools(VOICE_PROVIDER_PROFILES["xai"].session.to_dict())

    assert tools == [{"type": "function", **_DELEGATE}]


def test_a_spec_from_before_the_switch_reads_as_off():
    legacy = {"provider": "xai", "turn_ownership": "provider", "voice": "castor", "reasoning_effort": "high"}
    assert VoiceSessionSpec.from_dict(legacy).builtin_search is False


def test_the_wire_carries_the_switch_only_when_on():
    spec = VOICE_PROVIDER_PROFILES["xai"].session
    assert "builtin_search" not in spec.to_dict()
    on = VoiceSessionSpec.from_dict({**spec.to_dict(), "builtin_search": True})
    assert on.to_dict()["builtin_search"] is True
    assert VoiceSessionSpec.from_dict(on.to_dict()) == on


def test_an_adapter_without_built_in_search_refuses_the_switch():
    # Only the xAI profile can carry it; a spec that asks OpenAI for it fails at build, not silently.
    voice = {**VOICE_PROVIDER_PROFILES["openai"].session.to_dict(), "builtin_search": True}
    with pytest.raises(TypeError):
        relay_main._realtime_session_factory(_KEYS, MULAW_8K)(VoiceSessionSpec.from_dict(voice))
