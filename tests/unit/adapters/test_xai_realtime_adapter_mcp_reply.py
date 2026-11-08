"""Remote MCP calls and the reply that speaks their results, at the websocket boundary of the xAI adapter.

xAI runs a remote MCP call itself, and the response that made it can end (`response.done`, its output holding the
`mcp_call` item) before the call finishes; nothing speaks the result until the client sends `response.create`. The
adapter reports, on `response_done`, which of the response's calls are still running, and each finished call once as a
`remote_tool_done` port event. Event names follow the OpenAI Realtime MCP guide, which xAI's events follow.
"""
import json
import logging
from unittest.mock import AsyncMock

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer

FACTS = RemoteToolServer("alek_memory", "https://mcp.example.test/mcp", "The owner's memory.",
                         ("get_user_context",), token="tok-facts")
DRIVE = RemoteToolServer("alek_drive", "https://mcp.example.test/drive-mcp", "The owner's indexed files.",
                         ("search_drive",), token="tok-drive")
_RESULT = "Garage space 14 behind 12 Oak Street"
_ARGS = json.dumps({"query": "garage space lease"})


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


async def _events(incoming):
    ws = FakeWebSocket(list(incoming))
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[], mcp_servers=(FACTS, DRIVE))
    return [event async for event in adapter.receive_events()]


def _call_item(item_id="m1", label="alek_drive", **extra):
    return {"type": "mcp_call", "id": item_id, "server_label": label, "name": "search_drive", "arguments": _ARGS,
            **extra}


def _done(*output, status="completed"):
    return {"type": "response.done", "response": {"status": status, "output": list(output)}, "usage": {}}


_MESSAGE = {"type": "message", "role": "assistant", "content": [{"type": "output_audio", "transcript": "Checking."}]}


def _of(events, kind):
    return [event for event in events if event.type == kind]


class TestResponseDoneReportsTheCalls:
    async def test_a_response_holding_only_a_running_call_reports_it_pending(self):
        events = await _events([_done(_call_item(status="in_progress"))])

        assert [event.type for event in events] == ["response_done"]
        assert events[0].payload["remote_tools"] == {"calls": 1, "pending": ["m1"], "finished": [],
                                                     "continued": False, "cancelled": False}

    async def test_a_message_before_the_call_is_not_a_continuation(self):
        [done] = await _events([_done(_MESSAGE, _call_item(status="in_progress"))])

        assert done.payload["remote_tools"]["continued"] is False
        assert done.payload["remote_tools"]["pending"] == ["m1"]

    async def test_a_message_after_the_call_is_a_continuation(self):
        [done] = await _events([_done(_call_item(output=_RESULT), _MESSAGE)])

        assert done.payload["remote_tools"]["continued"] is True
        assert done.payload["remote_tools"]["pending"] == []

    async def test_a_call_already_carrying_its_output_is_finished_not_pending(self):
        [done] = await _events([_done(_call_item(item_id="m2", label="alek_memory", output=_RESULT))])

        assert done.payload["remote_tools"]["pending"] == []
        assert done.payload["remote_tools"]["finished"] == ["alek_memory"]

    async def test_a_cancelled_response_says_so(self):
        [done] = await _events([_done(_call_item(status="in_progress"), status="cancelled")])

        assert done.payload["remote_tools"]["cancelled"] is True

    async def test_a_response_without_calls_carries_no_remote_tools(self):
        [done] = await _events([_done(_MESSAGE)])

        assert "remote_tools" not in done.payload


class TestFinishedCalls:
    async def test_a_completed_event_after_response_done_is_one_remote_tool_done_with_the_items_label(self):
        events = await _events([
            {"type": "response.output_item.added", "item": _call_item(status="in_progress")},
            {"type": "response.mcp_call.in_progress", "item_id": "m1"},
            _done(_call_item(status="in_progress")),
            {"type": "response.mcp_call.completed", "item_id": "m1"},
        ])

        assert [event.type for event in events] == ["response_done", "remote_tool_done"]
        assert events[1].payload == {"item_id": "m1", "label": "alek_drive", "ok": True}

    async def test_a_failed_call_is_reported_not_ok(self):
        events = await _events([_done(_call_item(status="in_progress")),
                                {"type": "response.mcp_call.failed", "item_id": "m1"}])

        assert _of(events, "remote_tool_done")[0].payload["ok"] is False

    async def test_an_item_carrying_its_output_finishes_the_call_and_a_later_event_does_not_repeat_it(self):
        events = await _events([
            _done(_call_item(status="in_progress")),
            {"type": "conversation.item.done", "item": _call_item(output=_RESULT)},
            {"type": "response.mcp_call.completed", "item_id": "m1"},
            {"type": "response.output_item.done", "item": _call_item(output=_RESULT, status="completed")},
        ])

        assert len(_of(events, "remote_tool_done")) == 1

    async def test_an_item_without_its_result_does_not_finish_the_call(self):
        events = await _events([{"type": "response.output_item.done", "item": _call_item()}])

        assert events == []

    async def test_a_call_finished_before_its_response_ended_is_not_pending_there(self):
        events = await _events([
            {"type": "response.mcp_call.in_progress", "item_id": "m1"},
            {"type": "response.mcp_call.completed", "item_id": "m1"},
            _done(_call_item(status="in_progress")),
        ])

        assert events[0].type == "remote_tool_done"
        assert events[1].payload["remote_tools"]["pending"] == []

    async def test_a_label_the_session_never_sent_is_reported_as_unknown(self):
        events = await _events([{"type": "conversation.item.done",
                                 "item": _call_item(label="someone_else", output=_RESULT)}])

        assert events[0].payload["label"] == "unknown"


class TestLogLines:
    async def test_the_completed_line_names_the_server_and_the_duration_and_never_the_payload(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await _events([
                {"type": "response.mcp_call_arguments.done", "item_id": "m1", "arguments": _ARGS},
                _done(_call_item(status="in_progress")),
                {"type": "conversation.item.done", "item": _call_item(output=_RESULT, duration_ms=1500.4)},
            ])

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "mcp call completed: server=alek_drive ms=1500 (xai) ok=True" in text
        assert "Garage" not in text and "garage" not in text and "tok-" not in text

    async def test_without_a_reported_duration_the_adapter_measures_it(self, caplog):
        with caplog.at_level(logging.INFO):
            await _events([_done(_call_item(status="in_progress")),
                           {"type": "response.mcp_call.completed", "item_id": "m1"}])

        assert any("mcp call completed: server=alek_drive ms=" in r.getMessage() and "(since first event)"
                   in r.getMessage() for r in caplog.records)

    async def test_an_mcp_event_the_adapter_does_not_read_is_logged_by_type_only(self, caplog):
        with caplog.at_level(logging.INFO):
            events = await _events([{"type": "response.mcp_call.searching", "item_id": "m1", "output": _RESULT}])

        assert events == []
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "MCP event not read: response.mcp_call.searching" in text and "Garage" not in text
