"""OpenAI realtime sessions are never updated mid-call: interpreter mode, the only caller of update_session, runs on
xAI only. The adapter refuses and sends nothing on the wire."""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("changes", [
    pytest.param({"instructions": "base"}, id="instructions"),
    pytest.param({"tools": [{"name": "tool_a", "description": "d", "parameters": {"type": "object"}}]}, id="tools"),
    pytest.param({"tools": []}, id="withdraw"),
])
async def test_update_session_is_refused_and_sends_nothing(changes):
    ws = FakeWebSocket()
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="low", tools=[])
    ws.sent.clear()

    with pytest.raises(NotImplementedError):
        await adapter.update_session(**changes)

    assert ws.sent == []
