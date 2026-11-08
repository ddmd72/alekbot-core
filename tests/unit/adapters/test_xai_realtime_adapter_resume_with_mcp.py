"""A resumed xAI session still carries its MCP servers (LELIK_VOICE_MCP_RFC §4 with VOICE_CALL_CONTROL_CHANNEL_RFC §6).

The session.update follows the replay, so it must hold the `mcp` entries on a resume as on a fresh open; and xAI lists
the servers' tools at that session.update, so a failed list read while open() waits for session.updated is still
surfaced as `remote_tools_failed`. Ids, URLs and tokens are invented.
"""
import asyncio
import json
from unittest.mock import AsyncMock

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer
from tests.contracts.adapter_contracts import XAI_REALTIME_MCP_TOOLS

FACTS_TOKEN = "eyJhbGciOiJIUzI1NiIsImtpZCI6InZvaWNlLW1jcCJ9.eyJzdWIiOiJ1c3ItMSIsImF1ZCI6ImZhY3RzIn0.c2lnLWZhY3Rz"
DRIVE_TOKEN = "eyJhbGciOiJIUzI1NiIsImtpZCI6InZvaWNlLW1jcCJ9.eyJzdWIiOiJ1c3ItMSIsImF1ZCI6ImRyaXZlIn0.c2lnLWRyaXZl"
SERVERS = (
    RemoteToolServer("alek_memory", "https://mcp.example.test/mcp", "memory", ("get_user_context",), FACTS_TOKEN),
    RemoteToolServer("alek_drive", "https://mcp.example.test/drive-mcp", "drive",
                     ("search_drive", "find_passages", "read_passages"), DRIVE_TOKEN),
)
_CREATED = {"type": "conversation.created", "conversation": {"id": "conv-0001-invented"}}
_REPLAYED = {"type": "conversation.item.added", "replayed": True,
             "item": {"id": "i1", "role": "assistant", "content": [{"type": "text", "text": "Got it, lantern."}]}}
_LIST_FAILED = {"type": "mcp_list_tools.failed", "server_label": "alek_drive",
                "error": {"message": f"Cancelled by cancel scope ({FACTS_TOKEN})"}}
_UPDATED = {"type": "session.updated", "session": {}}


class _Socket:
    def __init__(self, incoming):
        self.sent, self._incoming = [], list(incoming)

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._incoming:
            await asyncio.sleep(3600)
        return json.dumps(self._incoming.pop(0))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def close(self):
        pass


def _entry(server: RemoteToolServer) -> dict:
    return {"type": "mcp", "server_url": server.url, "server_label": server.label,
            "server_description": server.description, "allowed_tools": list(server.allowed_tools),
            "authorization": server.token}


async def test_a_resumed_session_sends_its_mcp_servers_and_surfaces_a_list_failed_during_the_replay():
    ws = _Socket([_CREATED, _REPLAYED, _LIST_FAILED, _UPDATED])
    connect = AsyncMock(return_value=ws)
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=connect)

    await adapter.open(instructions="hi", reasoning_effort="high", tools=[], resume_handle="conv-0001-invented",
                       mcp_servers=SERVERS)

    assert adapter.resumed is True
    assert connect.await_args.args[0].endswith("&conversation_id=conv-0001-invented")
    XAI_REALTIME_MCP_TOOLS.validate("xai_realtime", {**ws.sent[0],
                                                     "expected_mcp_entries": [_entry(s) for s in SERVERS]})
    events = [event async for event in adapter.receive_events()]
    failed = [e for e in events if e.type == "remote_tools_failed"]
    assert [e.payload["label"] for e in failed] == ["alek_drive"]
    assert FACTS_TOKEN not in failed[0].payload["reason"]
    assert ("resume_handle", {"handle": "conv-0001-invented"}) in [(e.type, e.payload) for e in events]
