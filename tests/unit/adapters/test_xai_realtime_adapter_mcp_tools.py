"""Remote MCP servers in an xAI voice session, at the websocket boundary (LELIK_VOICE_MCP_RFC §4, §5.4; slice S3).

Shapes follow the live probe, phase A (2026-11-08): session.update takes one `mcp` entry per server next to the
function tools, `authorization` raw (xAI adds `Bearer ` itself); a failed tools/list arrives only as
`mcp_list_tools.failed` with an opaque error, no `error` event, and the session goes on. The adapter surfaces that as a
`remote_tools_failed` port event naming one of the session's labels, with no URL or token.
"""
import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer
from tests.contracts.adapter_contracts import XAI_REALTIME_MCP_TOOLS

FACTS_TOKEN = "eyJhbGciOiJIUzI1NiIsImtpZCI6InZvaWNlLW1jcCJ9.eyJzdWIiOiJ1c3ItMSIsImF1ZCI6ImZhY3RzIn0.c2lnLWZhY3Rz"
DRIVE_TOKEN = "eyJhbGciOiJIUzI1NiIsImtpZCI6InZvaWNlLW1jcCJ9.eyJzdWIiOiJ1c3ItMSIsImF1ZCI6ImRyaXZlIn0.c2lnLWRyaXZl"
FACTS = RemoteToolServer("alek_memory", "https://mcp.example.test/mcp", "The owner's memory.",
                         ("get_user_context",), token=FACTS_TOKEN)
DRIVE = RemoteToolServer("alek_drive", "https://mcp.example.test/drive-mcp", "The owner's indexed files.",
                         ("search_drive", "find_passages", "read_passages"), token=DRIVE_TOKEN)
_DELEGATE = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}


class FakeWebSocket:
    def __init__(self, incoming: list):
        self.sent: list = []
        self._incoming = incoming

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def close(self) -> None:
        pass


async def _opened(incoming=(), servers=(FACTS, DRIVE), tools=(_DELEGATE,)):
    ws = FakeWebSocket(list(incoming))
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="high", tools=list(tools), mcp_servers=servers)
    return adapter, ws


async def _events(adapter):
    return [event async for event in adapter.receive_events()]


def _expected_entry(server: RemoteToolServer) -> dict:
    return {"type": "mcp", "server_url": server.url, "server_label": server.label,
            "server_description": server.description, "allowed_tools": list(server.allowed_tools),
            "authorization": server.token}


class TestSessionUpdate:
    async def test_carries_one_mcp_entry_per_server_verbatim_with_the_raw_token(self):
        _, ws = await _opened()

        update = ws.sent[0]
        assert update["type"] == "session.update"
        XAI_REALTIME_MCP_TOOLS.validate("xai_realtime", {
            **update, "expected_mcp_entries": [_expected_entry(FACTS), _expected_entry(DRIVE)]})
        assert [t["authorization"] for t in update["session"]["tools"] if t["type"] == "mcp"] == [FACTS_TOKEN,
                                                                                                    DRIVE_TOKEN]
        assert [t["type"] for t in update["session"]["tools"]] == ["function", "mcp", "mcp"]

    async def test_servers_alone_still_open_the_tools_list(self):
        _, ws = await _opened(tools=())

        XAI_REALTIME_MCP_TOOLS.validate("xai_realtime", {
            **ws.sent[0], "expected_mcp_entries": [_expected_entry(FACTS), _expected_entry(DRIVE)]})

    async def test_without_servers_no_mcp_entry_is_sent(self):
        _, ws = await _opened(servers=())

        assert [t["type"] for t in ws.sent[0]["session"]["tools"]] == ["function"]

    async def test_opening_logs_nothing_that_carries_a_token_or_url(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await _opened()

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert FACTS_TOKEN not in text and DRIVE_TOKEN not in text and "mcp.example.test" not in text


class TestListToolsFailed:
    async def test_becomes_a_port_event_with_the_label_and_no_url_or_token(self):
        failed = {"type": "mcp_list_tools.failed", "event_id": "ev-1", "item_id": "item-mcp-1",
                  "server_label": "alek_memory",
                  "error": {"message": f"Cancelled by cancel scope at https://mcp.example.test/mcp "
                                       f"(Bearer {FACTS_TOKEN})"}}
        adapter, _ = await _opened([failed])

        [event] = await _events(adapter)

        assert event.type == "remote_tools_failed"
        assert event.payload["label"] == "alek_memory"
        assert "Cancelled by cancel scope" in event.payload["reason"]
        assert "mcp.example.test" not in event.payload["reason"] and FACTS_TOKEN not in event.payload["reason"]

    async def test_the_label_comes_from_the_list_item_when_the_event_names_only_the_item(self):
        events = [
            {"type": "conversation.item.added", "item": {"id": "item-mcp-2", "type": "mcp_list_tools",
                                                         "server_label": "alek_drive"}},
            {"type": "mcp_list_tools.failed", "item_id": "item-mcp-2", "error": "Cancelled by cancel scope"},
        ]
        adapter, _ = await _opened(events)

        [event] = await _events(adapter)

        assert event.type == "remote_tools_failed" and event.payload["label"] == "alek_drive"

    async def test_a_label_the_session_did_not_send_is_reported_as_unknown(self):
        failed = {"type": "mcp_list_tools.failed", "server_label": "https://mcp.example.test/mcp", "error": "x"}
        adapter, _ = await _opened([failed])

        [event] = await _events(adapter)

        assert event.payload["label"] == "unknown"

    async def test_list_tools_events_reach_no_port_event_a_finished_call_one_and_no_payload_is_logged(self, caplog):
        # The call events carry no server_label and no mcp_call item names one: the call's label is "unknown" (the
        # adapter never borrows the list-tools label: the session has two servers).
        events = [
            {"type": "mcp_list_tools.in_progress", "item_id": "i1", "server_label": "alek_memory"},
            {"type": "mcp_list_tools.completed", "item_id": "i1", "server_label": "alek_memory",
             "tools": [{"name": "get_user_context", "description": "Garage space 14 behind 12 Oak Street"}]},
            {"type": "response.mcp_call_arguments.delta", "item_id": "c1", "delta": "{\"query\": \"garage\"}"},
            {"type": "response.mcp_call.in_progress", "item_id": "c1"},
            {"type": "response.mcp_call.completed", "item_id": "c1",
             "output": "Garage space 14 behind 12 Oak Street"},
        ]
        adapter, _ = await _opened(events)

        with caplog.at_level(logging.DEBUG):
            received = await _events(adapter)

        assert [(event.type, event.payload) for event in received] == [
            ("remote_tool_done", {"item_id": "c1", "label": "unknown", "ok": True})]
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "Garage space" not in text and "garage" not in text
        assert "server=alek_memory" in text


class TestErrorEvent:
    async def test_an_error_echoing_the_session_payload_is_redacted_in_the_event_and_the_log(self, caplog):
        adapter, ws = await _opened()
        echoed = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad session",
                                             "param": json.dumps(ws.sent[0])}}
        ws._incoming.append(echoed)

        with caplog.at_level(logging.DEBUG):
            [event] = await _events(adapter)

        assert event.type == "error"
        text = "\n".join(r.getMessage() for r in caplog.records) + event.payload["message"]
        assert "invalid_request_error" in text
        assert FACTS_TOKEN not in text and DRIVE_TOKEN not in text and "mcp.example.test" not in text

    @pytest.mark.parametrize("echo", [
        {"authorization": FACTS_TOKEN, "server_url": "https://mcp.example.test/mcp"},
        f"tool alek_memory refused: Authorization: Bearer {FACTS_TOKEN} at https://mcp.example.test/mcp",
        {"authorization": "opaque-non-jwt-value", "server_label": "alek_memory"},
    ])
    async def test_a_short_echo_is_redacted_not_merely_cut(self, echo, caplog):
        adapter, _ = await _opened([{"type": "error", "error": {"message": "refused", "echo": echo}}])

        with caplog.at_level(logging.DEBUG):
            [event] = await _events(adapter)

        text = "\n".join(r.getMessage() for r in caplog.records) + event.payload["message"]
        assert "refused" in text
        assert FACTS_TOKEN not in text and "opaque-non-jwt-value" not in text and "mcp.example.test" not in text
