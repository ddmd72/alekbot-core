import json

import pytest
from unittest.mock import AsyncMock

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from tests.contracts.adapter_contracts import OPENAI_REALTIME_TOOLS_ARE_FUNCTION_SHAPED

_DECL = {
    "name": "delegate_to_specialist",
    "description": "Send a task to a specialist agent.",
    "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]},
}


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_neutral_declaration_is_sent_as_a_realtime_function():
    ws = FakeWebSocket()
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))

    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[_DECL])

    session = ws.sent[0]["session"]
    assert session["tools"] == [{"type": "function", **_DECL}]
    assert session["tool_choice"] == "auto"
    OPENAI_REALTIME_TOOLS_ARE_FUNCTION_SHAPED.validate("openai_realtime", ws.sent[0])


@pytest.mark.asyncio
async def test_no_tools_sends_no_tools_key():
    ws = FakeWebSocket()
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))

    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[])

    session = ws.sent[0]["session"]
    assert "tools" not in session
    assert "tool_choice" not in session
    OPENAI_REALTIME_TOOLS_ARE_FUNCTION_SHAPED.validate("openai_realtime", ws.sent[0])
