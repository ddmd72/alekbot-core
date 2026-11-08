"""xAI's real remote-MCP event sequence, at the websocket boundary (live probe, grok-voice-3.0, a public no-auth MCP
server; the fixtures below copy its shapes with invented ids and content).

What xAI sends for two parallel calls: `response.mcp_call_arguments.delta`/`.done` (item_id, call_id, name) and
`response.mcp_call.in_progress` (call_id, name) per call; the call items are `function_call` items named
`<server_label>.<tool>` (never `mcp_call`, no `server_label` field); `response.done` holds them before any result; then
per call a `function_call_output` conversation item and `response.mcp_call.completed` (call_id, name, output). Every
event names the call by `call_id`. The first relay build keyed calls by item id and `mcp_call` items, so it saw none of
this: the results went unspoken.
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import src.services.voice_session_service as voice_module
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.remote_tool_server import RemoteToolServer
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.services.voice_session_service import VoiceSessionService

FACTS = RemoteToolServer("alek_memory", "https://mcp.example.test/mcp", "The owner's memory.",
                         ("get_user_context",), token="tok-facts")
DRIVE = RemoteToolServer("alek_drive", "https://mcp.example.test/drive-mcp", "The owner's indexed files.",
                         ("search_drive",), token="tok-drive")
_RESULT = "Garage space 14 behind 12 Oak Street"
_RESP = "resp-1"
_CALLS = (("call-a-0", "item-a-0", "alek_memory.get_user_context", '{"query": "garage space"}'),
          ("call-a-1", "item-a-1", "alek_drive.search_drive", '{"query": "garage lease"}'))


def _function_call_item(call_id, item_id, name, role, status="in_progress", arguments=""):
    return {"id": item_id, "object": "realtime.item", "type": "function_call", "status": status, "role": role,
            "call_id": call_id, "name": name, "arguments": arguments, "replayed": False}


def _message(status):
    return {"id": "msg-1", "object": "realtime.item", "type": "message", "status": status, "role": "assistant",
            "replayed": False}


def _call_started():
    """xAI's events from response.created to response.done for two parallel MCP calls, in the probe's order."""
    events = [{"type": "response.created", "response": {"id": _RESP, "status": "in_progress", "output": []}},
              {"type": "response.output_item.added", "response_id": _RESP, "output_index": 0,
               "item": _message("in_progress")},
              {"type": "conversation.item.added", "item": _message("in_progress")}]
    for call_id, item_id, name, arguments in _CALLS:
        events += [{"type": "response.mcp_call_arguments.delta", "item_id": item_id, "call_id": call_id,
                    "delta": arguments},
                   {"type": "response.mcp_call_arguments.done", "item_id": item_id, "response_id": _RESP,
                    "output_index": 0, "call_id": call_id, "name": name, "arguments": arguments},
                   {"type": "response.mcp_call.in_progress", "call_id": call_id, "name": name}]
    events.append({"type": "response.output_item.done", "response_id": _RESP, "output_index": 0,
                   "item": _message("completed")})
    for call_id, item_id, name, _ in _CALLS:
        events += [{"type": "response.output_item.added", "response_id": _RESP, "output_index": 0,
                    "item": _function_call_item(call_id, item_id, name, "user")},
                   {"type": "conversation.item.added", "item": _function_call_item(call_id, item_id, name, "tool")}]
    events.append({"type": "response.done", "response_id": _RESP, "usage": {},
                   "response": {"id": _RESP, "status": "completed", "output": [
                       _message("completed"),
                       *(_function_call_item(c, i, n, "user", "completed", a) for c, i, n, a in _CALLS)]}})
    return events


def _call_finished(index):
    call_id, _, name, _ = _CALLS[index]
    return [{"type": "conversation.item.added", "item": {
                "id": f"out-{index}", "object": "realtime.item", "type": "function_call_output", "status": "completed",
                "role": "tool", "call_id": call_id, "output": _RESULT, "replayed": False}},
            {"type": "response.mcp_call.completed", "call_id": call_id, "name": name, "output": _RESULT}]


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


async def _events(incoming, tools=()):
    ws = FakeWebSocket(list(incoming))
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))
    await adapter.open(instructions="hi", reasoning_effort="high", tools=list(tools), mcp_servers=(FACTS, DRIVE))
    return [event async for event in adapter.receive_events()]


class TestTheRealSequence:
    async def test_response_done_reports_both_calls_running_and_no_tool_call_reaches_the_relay(self):
        events = await _events(_call_started())

        assert [event.type for event in events] == ["response_created", "response_done"]
        assert events[1].payload["remote_tools"] == {"calls": 2, "pending": ["call-a-0", "call-a-1"],
                                                     "finished": [], "continued": False, "cancelled": False}

    async def test_each_finished_call_is_one_remote_tool_done_with_its_label(self):
        events = await _events([*_call_started(), *_call_finished(0), *_call_finished(1)])

        done = [event.payload for event in events if event.type == "remote_tool_done"]
        assert done == [{"item_id": "call-a-0", "label": "alek_memory", "ok": True},
                        {"item_id": "call-a-1", "label": "alek_drive", "ok": True}]

    async def test_the_completed_event_alone_finishes_a_call(self):
        call_id, _, name, _ = _CALLS[1]
        events = await _events([*_call_started(),
                                {"type": "response.mcp_call.completed", "call_id": call_id, "name": name}])

        assert [e.payload["label"] for e in events if e.type == "remote_tool_done"] == ["alek_drive"]

    async def test_a_failed_event_finishes_a_call_not_ok(self):
        call_id, _, name, _ = _CALLS[0]
        events = await _events([*_call_started(),
                                {"type": "response.mcp_call.failed", "call_id": call_id, "name": name}])

        assert [e.payload["ok"] for e in events if e.type == "remote_tool_done"] == [False]

    async def test_an_mcp_named_function_call_arguments_event_is_never_a_tool_call(self):
        call_id, item_id, name, arguments = _CALLS[1]
        events = await _events([{"type": "response.function_call_arguments.done", "item_id": item_id,
                                 "call_id": call_id, "name": name, "arguments": arguments}])

        assert events == []

    async def test_the_relays_own_function_call_and_its_output_are_not_mcp_calls(self):
        own = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}
        events = await _events([
            {"type": "response.function_call_arguments.done", "call_id": "call-own", "name": "delegate_to_specialist",
             "arguments": "{}"},
            {"type": "response.done", "usage": {}, "response": {"status": "completed", "output": [
                _function_call_item("call-own", "item-own", "delegate_to_specialist", "user", "completed")]}},
            {"type": "conversation.item.added", "item": {"type": "function_call_output", "call_id": "call-own",
                                                         "output": "sunny"}},
        ], tools=(own,))

        assert [event.type for event in events] == ["tool_call", "response_done"]
        assert "remote_tools" not in events[1].payload

    async def test_calls_seen_by_their_events_are_reported_even_when_the_output_does_not_show_them(self):
        """The safety net: a call item of an unknown shape still leaves its call pending on response_done."""
        events = await _events([
            {"type": "response.created", "response": {}},
            {"type": "response.mcp_call.in_progress", "call_id": "call-x", "name": "other_server.lookup"},
            {"type": "response.done", "usage": {}, "response": {"status": "completed", "output": [
                {"id": "item-x", "type": "remote_call", "status": "in_progress"}]}},
            {"type": "response.mcp_call.completed", "call_id": "call-x", "name": "other_server.lookup"},
        ])

        assert events[1].payload["remote_tools"]["pending"] == ["call-x"]
        assert events[2].type == "remote_tool_done" and events[2].payload["label"] == "unknown"

    async def test_a_later_response_does_not_report_an_earlier_responses_calls(self):
        events = await _events([*_call_started(), *_call_finished(0), *_call_finished(1),
                                {"type": "response.created", "response": {}},
                                {"type": "response.done", "usage": {},
                                 "response": {"status": "completed", "output": [_message("completed")]}}])

        assert "remote_tools" not in events[-1].payload


class TestLogLines:
    async def test_each_completion_names_its_server_and_no_line_holds_a_payload(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await _events([*_call_started(), *_call_finished(0), *_call_finished(1)])

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "mcp call completed: server=alek_memory" in text and "mcp call completed: server=alek_drive" in text
        assert "Garage" not in text and "garage" not in text and "tok-" not in text
        assert "not read" not in text  # every event of the real sequence is read

    async def test_an_output_item_type_the_adapter_does_not_know_is_logged_by_type(self, caplog):
        with caplog.at_level(logging.INFO):
            await _events([
                {"type": "response.output_item.added", "item": {"type": "remote_call", "output": _RESULT}},
                {"type": "response.done", "usage": {}, "response": {"status": "completed", "output": [
                    {"type": "tool_invocation", "output": _RESULT}]}},
            ])

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "output item type not read: remote_call" in text
        assert "output item type not read: tool_invocation" in text
        assert "Garage" not in text


class _QuietingWebSocket(FakeWebSocket):
    async def __anext__(self):
        if not self._incoming:
            await asyncio.sleep(10)  # the provider goes quiet; the call ends with the inbound stream
        return json.dumps(self._incoming.pop(0))


class TestUnderTheSessionLoop:
    async def test_the_real_sequence_gets_one_response_create_after_the_last_call_finished(self):
        ws = _QuietingWebSocket([{"type": "response.created", "response": {}},
                                 {"type": "response.done", "usage": {},
                                  "response": {"status": "completed", "output": []}},
                                 *_call_started(), *_call_finished(0), *_call_finished(1)])
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))
        sent_when_second_result_arrives: list = []
        read = adapter._normalize

        def normalize(event):
            item = event.get("item") or {}
            if item.get("type") == "function_call_output" and item.get("call_id") == "call-a-1":
                sent_when_second_result_arrives.append(len(ws.sent))
            return read(event)

        adapter._normalize = normalize  # type: ignore[method-assign]
        control = AsyncMock()
        control.fetch_session_config.return_value = {
            "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": [],
            "voice": VOICE_PROVIDER_PROFILES["xai"].session.to_dict()}

        async def inbound():
            await asyncio.sleep(0.3)
            return
            yield  # pragma: no cover

        service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=adapter),
                                      control_plane=control, alert_sink=AsyncMock())
        await service._run_call("t1", control.fetch_session_config.return_value,
                                VOICE_PROVIDER_PROFILES["xai"].session, inbound(), AsyncMock(), AsyncMock(),
                                _state(), mcp_servers=(FACTS, DRIVE))

        creates = [index for index, sent in enumerate(ws.sent) if sent["type"] == "response.create"]
        assert len(creates) == 2  # the pickup, then one reply once both calls finished
        assert creates[1] >= sent_when_second_result_arrives[0]  # never after the first call alone
        assert creates[1] == len(ws.sent) - 1


def _state():
    return voice_module._CallState(playback=PlaybackTracker(), ticket="t1",
                                   turn_ownership=VOICE_PROVIDER_PROFILES["xai"].session.turn_ownership)
