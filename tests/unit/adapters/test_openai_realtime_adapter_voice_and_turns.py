"""OpenAIRealtimeAdapter: voice from the call's spec, RELAY turn ownership only."""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.domain.voice_turn_ownership import TurnOwnership


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class TestOpenAIVoiceAndTurns:
    def test_declares_relay_ownership_only(self):
        assert OpenAIRealtimeAdapter.supported_turn_ownership == frozenset({TurnOwnership.RELAY})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs,expected", [({}, "verse"), ({"voice": "cedar"}, "cedar")])
    async def test_voice_defaults_to_verse_and_follows_the_spec(self, kwargs, expected):
        ws = _Ws()
        adapter = OpenAIRealtimeAdapter(api_key="k", ws_connect=AsyncMock(return_value=ws), **kwargs)
        await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])
        assert ws.sent[0]["session"]["audio"]["output"]["voice"] == expected
