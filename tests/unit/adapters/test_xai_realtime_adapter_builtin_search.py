"""xAI's server-side web_search / x_search in a voice session, at the websocket boundary.

Event shapes follow the live probe of 2026-11-08 (grok-voice-3.0): a built-in search arrives as an ordinary
function_call item named after the tool, at the end of the response, and is listed in response.done's output.
xAI runs it itself: the relay must not see a tool_call for it, and billing counts it per response.
"""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from tests.contracts.adapter_contracts import XAI_REALTIME_BUILTIN_SEARCH_TOOLS

_DELEGATE = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}
_SEND_TO_CHAT = {"name": "send_to_chat", "description": "c", "parameters": {"type": "object"}}
_USAGE = {
    "input_tokens": 279,
    "input_token_details": {"text_tokens": 15, "audio_tokens": 264, "grok_tokens": 0},
    "output_tokens": 1731,
    "output_token_details": {"text_tokens": 119, "audio_tokens": 1612, "grok_tokens": 0},
    "total_tokens": 2010,
    "output_audio_seconds": 32.24,
    "billable_audio_seconds": 10,
}


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self.sent: list[dict] = []
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


async def _opened(incoming: list[dict], tools: list[dict],
                  builtin_search: bool = True) -> tuple[XaiRealtimeAdapter, FakeWebSocket]:
    ws = FakeWebSocket(incoming=incoming)
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws),
                                 builtin_search=builtin_search)
    await adapter.open(instructions="hi", reasoning_effort="high", tools=tools)
    return adapter, ws


async def _events(adapter: XaiRealtimeAdapter) -> list:
    return [event async for event in adapter.receive_events()]


def _call_item(name: str, call_id: str, status: str = "completed") -> dict:
    return {"id": f"item-{call_id}", "object": "realtime.item", "type": "function_call", "status": status,
            "role": "user", "call_id": call_id, "name": name, "arguments": "{\"query\":\"q\"}", "replayed": False}


def _function_call_events(name: str, call_id: str) -> list[dict]:
    """The events one function call produces, in the order the probe recorded them."""
    return [
        {"type": "response.output_item.added", "response_id": "r1", "item": _call_item(name, call_id, "in_progress")},
        {"type": "conversation.item.added", "item": _call_item(name, call_id, "in_progress")},
        {"type": "response.function_call_arguments.delta", "response_id": "r1", "item_id": f"item-{call_id}",
         "call_id": call_id, "delta": "{\"query\":\"q\"}"},
        {"type": "response.function_call_arguments.done", "response_id": "r1", "item_id": f"item-{call_id}",
         "call_id": call_id, "name": name, "arguments": "{\"query\":\"q\"}"},
        {"type": "response.output_item.done", "response_id": "r1", "item": _call_item(name, call_id)},
    ]


def _response(calls: list[tuple[str, str]]) -> list[dict]:
    """One spoken reply followed by the given function calls, closed by response.done listing them in output."""
    message = {"id": "m1", "object": "realtime.item", "type": "message", "status": "completed", "role": "assistant",
               "content": [{"type": "audio", "transcript": "The rain stops by noon."}]}
    events = [
        {"type": "response.created", "response": {"id": "r1", "status": "in_progress", "output": []}},
        {"type": "response.output_audio_transcript.done", "response_id": "r1", "item_id": "m1",
         "transcript": "The rain stops by noon."},
    ]
    for name, call_id in calls:
        events += _function_call_events(name, call_id)
    events.append({
        "type": "response.done", "response_id": "r1", "usage": _USAGE,
        "response": {"id": "r1", "status": "completed", "usage": {},
                     "output": [message] + [_call_item(name, call_id) for name, call_id in calls]},
    })
    return events


class TestSessionOffersTheBuiltInSearches:
    @pytest.mark.asyncio
    async def test_session_update_carries_both_built_ins_bare_next_to_the_function_tools(self):
        _, ws = await _opened([], tools=[_DELEGATE, _SEND_TO_CHAT])

        sent = ws.sent[0]
        assert sent["type"] == "session.update"
        assert sent["session"]["tools"] == [
            {"type": "web_search"}, {"type": "x_search"},
            {"type": "function", **_DELEGATE}, {"type": "function", **_SEND_TO_CHAT},
        ]
        XAI_REALTIME_BUILTIN_SEARCH_TOOLS.validate(
            "xai_realtime", {**sent, "expected_function_names": ["delegate_to_specialist", "send_to_chat"]})

    @pytest.mark.asyncio
    async def test_built_ins_and_tool_choice_are_sent_when_the_caller_has_no_tools(self):
        _, ws = await _opened([], tools=[])

        session = ws.sent[0]["session"]
        assert session["tools"] == [{"type": "web_search"}, {"type": "x_search"}]
        assert session["tool_choice"] == "auto"
        XAI_REALTIME_BUILTIN_SEARCH_TOOLS.validate("xai_realtime", {**ws.sent[0], "expected_function_names": []})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("builtin_search", [True, False])
    @pytest.mark.parametrize("name", ["web_search", "x_search"])
    async def test_a_function_tool_named_like_a_built_in_is_refused(self, name, builtin_search):
        # Its calls would be taken for xAI's own and never answered, switch on or off.
        with pytest.raises(ValueError, match=name):
            await _opened([], tools=[{**_DELEGATE, "name": name}], builtin_search=builtin_search)


class TestSwitchedOffSessionIsUnchanged:
    @pytest.mark.asyncio
    async def test_off_by_default_offers_only_the_function_tools(self):
        ws = FakeWebSocket(incoming=[])
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))
        await adapter.open(instructions="hi", reasoning_effort="high", tools=[_DELEGATE])

        session = ws.sent[0]["session"]
        assert session["tools"] == [{"type": "function", **_DELEGATE}]
        assert session["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_off_with_no_tools_sends_no_tools_at_all(self):
        _, ws = await _opened([], tools=[], builtin_search=False)

        session = ws.sent[0]["session"]
        assert "tools" not in session and "tool_choice" not in session


class TestBuiltInCallsAreNotOurs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("builtin_search", [True, False])
    @pytest.mark.parametrize("name,leg", [("web_search", "web_search_calls"), ("x_search", "x_search_calls")])
    async def test_a_built_in_call_raises_no_tool_call_and_counts_once(self, name, leg, builtin_search):
        # Switched off, xAI should never run one; if it does, it is still not ours to answer, and it is still billed.
        adapter, ws = await _opened(_response([(name, "call-0")]), tools=[_DELEGATE], builtin_search=builtin_search)
        ws.sent.clear()

        events = await _events(adapter)

        assert [e.type for e in events] == ["response_created", "model_transcript", "response_done"]
        usage = events[-1].payload["usage"]
        assert usage[leg] == 1
        assert ws.sent == []  # no function_call_output, no response.create

    @pytest.mark.asyncio
    async def test_several_searches_in_one_response_are_counted_per_tool(self):
        # The probe's reply ran web_search twice in one response.
        adapter, _ = await _opened(
            _response([("web_search", "c0"), ("web_search", "c1"), ("x_search", "c2")]), tools=[_DELEGATE])

        usage = (await _events(adapter))[-1].payload["usage"]

        assert (usage["web_search_calls"], usage["x_search_calls"]) == (2, 1)

    @pytest.mark.asyncio
    async def test_our_own_function_call_still_raises_a_tool_call_and_is_not_counted(self):
        adapter, _ = await _opened(
            _response([("web_search", "c0"), ("delegate_to_specialist", "c1")]), tools=[_DELEGATE])

        events = await _events(adapter)

        tool_calls = [e.payload for e in events if e.type == "tool_call"]
        assert tool_calls == [{"call_id": "c1", "name": "delegate_to_specialist", "arguments": "{\"query\":\"q\"}"}]
        usage = events[-1].payload["usage"]
        assert usage["web_search_calls"] == 1
        assert "x_search_calls" not in usage

    @pytest.mark.asyncio
    async def test_a_response_without_searches_carries_no_search_legs(self):
        adapter, _ = await _opened(_response([]), tools=[_DELEGATE])

        usage = (await _events(adapter))[-1].payload["usage"]

        assert not {"web_search_calls", "x_search_calls"} & set(usage)

    @pytest.mark.asyncio
    async def test_search_counts_are_per_response_not_a_running_total(self):
        adapter, _ = await _opened(
            _response([("web_search", "c0")]) + _response([("web_search", "c1")]), tools=[_DELEGATE])

        done = [e for e in await _events(adapter) if e.type == "response_done"]

        assert [e.payload["usage"]["web_search_calls"] for e in done] == [1, 1]
