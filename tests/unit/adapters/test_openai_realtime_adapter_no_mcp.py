"""OpenAI realtime sessions carry no remote MCP servers (LELIK_VOICE_MCP_RFC O6: xAI only), at the websocket boundary.

A server handed to the OpenAI adapter is refused before any connection, so its token never reaches OpenAI.
"""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer

_DECL = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}
_SERVER = RemoteToolServer("alek_memory", "https://mcp.example.test/mcp", "memory", ("get_user_context",),
                           token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c3ItMSJ9.c2ln")


class FakeWebSocket:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        pass


async def test_an_openai_session_update_has_no_mcp_entry():
    ws = FakeWebSocket()
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))

    await adapter.open(instructions="hi", reasoning_effort="medium", tools=[_DECL])

    assert [tool["type"] for tool in ws.sent[0]["session"]["tools"]] == ["function"]


async def test_a_server_is_refused_before_connecting():
    connect = AsyncMock(return_value=FakeWebSocket())
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=connect)

    with pytest.raises(ValueError) as refused:
        await adapter.open(instructions="hi", reasoning_effort="medium", tools=[_DECL], mcp_servers=[_SERVER])

    connect.assert_not_awaited()
    assert _SERVER.token not in str(refused.value)
