"""Wire tests: the string ``deliver_response`` path of ClaudeAdapter.

Spec: docs/04_solution_strategy/decisions/claude_deliver_response_string_tool.md.
A request with a dict ``response_schema``, at least one client tool and no grounding sends NO
``output_config.format``; a synthesized ``deliver_response(response: string)`` is appended last and the
model's call comes back unparsed as ``ToolCall("deliver_response", {"response": <raw>})``.

Mock boundary: ``AsyncAnthropic.messages.stream`` (a capturing callable), per
docs/how_to/ADAPTER_WIRE_TESTING.md. The truncation tests also feed the SDK's own streaming accumulator.
"""
import copy
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.agent import AgentResponse
from src.domain.llm import FinishReason
from src.domain.turn_clock import CURRENT_TURN_CLOCK, WRAP_UP_NOTE, TurnClock
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DelegationEngine
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import (
    SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL,
    SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT,
)

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_TOOLS = [
    {"name": "search_memory", "description": "Search memories",
     "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "delegate_to_specialist", "description": "Delegate",
     "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}}},
]
_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "link_list": {"type": "array", "items": {"type": "string"}}},
    "required": ["text"],
}
_ANSWER = '{"text": "hello", "link_list": []}'

_EXPECTED_DELIVER = {
    "name": "deliver_response",
    "description": (
        "Deliver your reply to the owner. This is how you finish: call it when no further "
        "specialist work is needed. Call it alone, without other tools."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "response": {
                "type": "string",
                "description": (
                    "Your whole reply: the JSON object described in the response format, "
                    "serialized as a string."
                ),
            },
        },
        "required": ["response"],
    },
}


# --------------------------------------------------------------------------- SDK-boundary helpers

class _FakeAPIStatusError(anthropic.APIStatusError):
    """APIStatusError with a controllable status_code (bypasses the httpx-bound __init__)."""

    def __init__(self, message, status_code=None):
        Exception.__init__(self, message)
        self.status_code = status_code


def _make_raising_cm(exc):
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(side_effect=exc)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _snap(kwargs):
    """Per-call copy of the wire kwargs. `messages` can hold SDK-shaped mocks (raw_content), so only the
    JSON-like request fields are deep-copied (the same dict is later mutated by the adapter's fallback)."""
    out = dict(kwargs)
    for key in ("tools", "system", "thinking", "output_config", "extra_headers", "tool_choice"):
        if key in out:
            out[key] = copy.deepcopy(out[key])
    out["messages"] = list(kwargs["messages"])
    return out

def _usage(output_tokens=5):
    return MagicMock(input_tokens=10, output_tokens=output_tokens,
                     cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _text_block(text):
    return MagicMock(type="text", text=text)


def _tool_block(name, args, tc_id="tu_1"):
    block = MagicMock(type="tool_use", input=args, id=tc_id)
    block.name = name  # MagicMock(name=...) names the mock, it does not set .name
    return block


def _response(*blocks, stop_reason="tool_use", output_tokens=5):
    return MagicMock(content=list(blocks), usage=_usage(output_tokens), stop_reason=stop_reason)


def _cm(sdk_response):
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(return_value=sdk_response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _install(adapter, *responses):
    """Capture a deep copy of the kwargs of every stream() call; answer with `responses` in order."""
    calls = []
    queue = list(responses)

    def stream(**kwargs):
        calls.append(_snap(kwargs))
        return _cm(queue.pop(0) if len(queue) > 1 else queue[0])

    adapter.client.messages.stream = stream
    return calls


def _req(model="claude-sonnet-5-5", tools=_TOOLS, schema=_SCHEMA, **kw):
    return LLMRequest(model_name=model, system_instruction="sys", messages=_MESSAGES,
                      tools=tools, response_schema=schema, **kw)


# --------------------------------------------------------------------------- request shape

async def test_schema_with_tools_sends_deliver_tool_and_no_format():
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block("ok"), stop_reason="end_turn"))
    await adapter.generate_content(_req(thinking="medium"))
    kw = calls[0]
    assert "format" not in kw["output_config"]
    assert kw["output_config"]["effort"] == "medium"
    assert kw["thinking"] == {"type": "adaptive"}
    assert [t["name"] for t in kw["tools"]] == ["search_memory", "delegate_to_specialist", "deliver_response"]
    assert kw["tools"][-1] == _EXPECTED_DELIVER
    assert "strict" not in kw["tools"][-1] and "cache_control" not in kw["tools"][-1]
    assert "tool_choice" not in kw
    SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL.validate("claude", {**kw, "expected_schema": _SCHEMA})


async def test_schema_with_tools_and_no_thinking_has_no_format():
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block("ok"), stop_reason="end_turn"))
    await adapter.generate_content(_req(model="claude-sonnet-4-6"))
    kw = calls[0]
    assert "format" not in (kw.get("output_config") or {})
    assert kw["tools"][-1] == _EXPECTED_DELIVER
    SCHEMA_WITH_CLIENT_TOOLS_USES_DELIVER_TOOL.validate("claude", {**kw, "expected_schema": _SCHEMA})


async def test_schema_without_tools_keeps_native_format():
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block(_ANSWER), stop_reason="end_turn"))
    await adapter.generate_content(_req(tools=None, thinking="low"))
    kw = calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert all(t["name"] != "deliver_response" for t in kw["tools"])
    SCHEMA_WITHOUT_TOOLS_USES_NATIVE_FORMAT.validate("claude", kw)


async def test_tools_without_schema_get_neither_format_nor_deliver_tool():
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block("ok"), stop_reason="end_turn"))
    await adapter.generate_content(_req(schema=None, thinking="medium"))
    kw = calls[0]
    assert "format" not in kw["output_config"]
    assert [t["name"] for t in kw["tools"]] == ["search_memory", "delegate_to_specialist"]


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-4-5"])
async def test_grounded_requests_never_take_the_deliver_path(model):
    """Sonnet: dynamic search (grounded loop); Haiku: legacy search tools through the plain path."""
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block(_ANSWER), stop_reason="end_turn"))

    async def fake_loop(create_kwargs):
        calls.append(_snap(create_kwargs))
        from src.ports.llm_port import LLMResponse
        return LLMResponse(text=_ANSWER, tool_calls=[], finish_reason=FinishReason.STOP)

    adapter._grounded_stream_loop = fake_loop
    await adapter.generate_content(_req(model=model, use_grounding=True))
    kw = calls[0]
    assert all(t["name"] != "deliver_response" for t in kw["tools"])
    assert kw["output_config"]["format"]["type"] == "json_schema"


async def test_client_tool_named_deliver_response_is_not_duplicated(caplog):
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block("ok"), stop_reason="end_turn"))
    own = {"name": "deliver_response", "description": "mine", "parameters": {"type": "object", "properties": {}}}
    with caplog.at_level(logging.WARNING):
        await adapter.generate_content(_req(tools=[*_TOOLS, own]))
    names = [t["name"] for t in calls[0]["tools"]]
    assert names.count("deliver_response") == 1
    assert calls[0]["tools"][-1]["description"] == "mine"
    assert "format" not in (calls[0].get("output_config") or {})
    assert any("already named" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


async def test_deliver_tool_definition_is_byte_identical_across_requests():
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block("ok"), stop_reason="end_turn"))
    await adapter.generate_content(_req(thinking="medium"))
    await adapter.generate_content(_req(thinking="low", schema={**_SCHEMA, "required": []}))
    assert json.dumps(calls[0]["tools"][-1], sort_keys=True) == json.dumps(calls[1]["tools"][-1], sort_keys=True)
    assert json.dumps(calls[0]["tools"][-1]) == json.dumps(calls[1]["tools"][-1])


async def test_deliver_tool_is_a_copy_so_one_request_cannot_change_the_next():
    adapter = ClaudeAdapter(api_key="k")
    seen = []

    def stream(**kwargs):
        seen.append(copy.deepcopy(kwargs["tools"][-1]))
        kwargs["tools"][-1]["description"] = "mutated by the caller"
        return _cm(_response(_text_block("ok"), stop_reason="end_turn"))

    adapter.client.messages.stream = stream
    await adapter.generate_content(_req())
    await adapter.generate_content(_req())
    assert seen[1] == _EXPECTED_DELIVER


async def test_same_provider_fallback_has_no_format_and_the_same_tools():
    adapter = ClaudeAdapter(api_key="k")
    calls = []
    exc = _FakeAPIStatusError("server error", status_code=500)

    def stream(**kwargs):
        calls.append(_snap(kwargs))
        if kwargs["model"] == "claude-sonnet-5-5":
            return _make_raising_cm(exc)
        return _cm(_response(_tool_block("deliver_response", {"response": _ANSWER})))

    adapter.client.messages.stream = stream
    result = await adapter.generate_content(_req(thinking="low"))
    assert [c["model"] for c in calls] == ["claude-sonnet-5-5", "claude-sonnet-5"]
    assert "format" not in calls[1]["output_config"]
    assert calls[1]["output_config"]["effort"] == "low"
    assert calls[1]["thinking"] == {"type": "adaptive"}
    assert calls[1]["tools"] == calls[0]["tools"] and calls[1]["tools"][-1] == _EXPECTED_DELIVER
    assert result.tool_calls[0].args == {"response": _ANSWER}


async def test_pdf_next_to_schema_and_tools_keeps_citations_and_sends_no_format():
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(adapter, _response(_text_block("ok"), stop_reason="end_turn"))
    pdf = {"base64": "JVBERi0xLjQK", "mime_type": "application/pdf", "name": "w.pdf"}
    messages = [Message(role="user", parts=[MessagePart(file_data=pdf), MessagePart(text="Read it.")])]
    await adapter.generate_content(LLMRequest(model_name="claude-sonnet-5-5", messages=messages,
                                              tools=_TOOLS, response_schema=_SCHEMA))
    kw = calls[0]
    doc = kw["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["citations"] == {"enabled": True}
    assert "format" not in (kw.get("output_config") or {})


# --------------------------------------------------------------------------- response parsing

async def test_deliver_call_with_a_string_is_returned_unparsed():
    adapter = ClaudeAdapter(api_key="k")
    raw = '```json\n{"text": "a", }\n``` trailing'  # deliberately not valid JSON: the adapter must not touch it
    _install(adapter, _response(_tool_block("deliver_response", {"response": raw}, tc_id="tu_42")))
    result = await adapter.generate_content(_req())
    assert [(c.name, c.args, c.thought_signature) for c in result.tool_calls] == [
        ("deliver_response", {"response": raw}, "tu_42")]


async def test_deliver_call_with_a_dict_response_is_passed_through():
    adapter = ClaudeAdapter(api_key="k")
    answer = {"text": "a", "link_list": []}
    _install(adapter, _response(_tool_block("deliver_response", {"response": answer})))
    result = await adapter.generate_content(_req())
    assert result.tool_calls[0].args == {"response": answer}


async def test_extra_keys_beside_response_are_dropped():
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", {"response": _ANSWER, "note": "x"})))
    result = await adapter.generate_content(_req())
    assert result.tool_calls[0].args == {"response": _ANSWER}


async def test_deliver_call_without_response_key_keeps_its_input():
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", {"text": "x"})))
    result = await adapter.generate_content(_req())
    assert result.tool_calls[0].args == {"text": "x"}


async def test_non_object_deliver_input_becomes_empty_args_with_a_warning(caplog):
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", "just a string"), stop_reason="tool_use"))
    with caplog.at_level(logging.WARNING):
        result = await adapter.generate_content(_req())
    assert [(c.name, c.args) for c in result.tool_calls] == [("deliver_response", {})]
    assert any(r.levelno == logging.WARNING and "outcome=non_dict_input" in r.getMessage()
               and "input_type=str" in r.getMessage() for r in caplog.records)


async def test_raw_content_is_the_sdk_response_with_thinking_and_tool_use_blocks():
    """The engine echoes raw_content back into history; Claude's preserved thinking needs the blocks,
    thinking signature included, exactly as the SDK returned them."""
    adapter = ClaudeAdapter(api_key="k")
    thinking = MagicMock(type="thinking", thinking="reasoning", signature="sig_abc")
    deliver = _tool_block("deliver_response", {"response": _ANSWER}, tc_id="tu_7")
    sdk_response = _response(thinking, deliver)
    _install(adapter, sdk_response)
    result = await adapter.generate_content(_req())
    assert result.raw_content is sdk_response
    assert result.raw_content.content == [thinking, deliver]
    assert result.raw_content.content[0].signature == "sig_abc"


async def test_other_tool_calls_are_untouched():
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("search_memory", {"q": "x"}, tc_id="tu_9")))
    result = await adapter.generate_content(_req())
    assert [(c.name, c.args, c.thought_signature) for c in result.tool_calls] == [
        ("search_memory", {"q": "x"}, "tu_9")]


async def test_without_the_deliver_path_a_deliver_named_call_is_not_special():
    """No schema -> deliver_path False: even a max_tokens cut with an empty input is passed as is."""
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", {}), stop_reason="max_tokens"))
    result = await adapter.generate_content(_req(schema=None))
    assert [c.name for c in result.tool_calls] == ["deliver_response"] and result.tool_calls[0].args == {}


# --------------------------------------------------------------------------- truncation

@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
@pytest.mark.parametrize("cut_input", [{}, {"response": ""}, {"response": 5}, {"response": None}, None])
async def test_truncated_deliver_call_drops_every_tool_call_and_keeps_text(stop_reason, cut_input, caplog):
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(
        _text_block("partial words"),
        _tool_block("delegate_to_specialist", {"intent": "search_web"}, tc_id="tu_a"),
        _tool_block("deliver_response", cut_input, tc_id="tu_b"),
        stop_reason=stop_reason, output_tokens=16000))
    with caplog.at_level(logging.ERROR):
        result = await adapter.generate_content(_req())
    assert result.tool_calls == []
    assert result.text == "partial words"
    assert result.finish_reason == FinishReason.MAX_TOKENS
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("outcome=truncated" in m and "source=deliver" in m and "16000" in m
               and "delegate_to_specialist" in m and "deliver_response" in m for m in errors)


async def test_max_tokens_with_a_usable_string_is_kept():
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", {"response": _ANSWER}), stop_reason="max_tokens"))
    result = await adapter.generate_content(_req())
    assert result.tool_calls[0].args == {"response": _ANSWER}
    assert result.finish_reason == FinishReason.MAX_TOKENS


async def test_max_tokens_with_a_non_empty_dict_is_kept():
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", {"response": {"text": "a"}}),
                                stop_reason="max_tokens"))
    result = await adapter.generate_content(_req())
    assert result.tool_calls[0].args == {"response": {"text": "a"}}


async def test_end_turn_with_empty_input_is_kept_as_is():
    """The truncation rule is max_tokens-only."""
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("deliver_response", {}), stop_reason="end_turn"))
    result = await adapter.generate_content(_req())
    assert [(c.name, c.args) for c in result.tool_calls] == [("deliver_response", {})]


async def test_truncated_sibling_without_a_bad_deliver_call_is_not_dropped():
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, _response(_tool_block("delegate_to_specialist", {}), stop_reason="max_tokens"))
    result = await adapter.generate_content(_req())
    assert [c.name for c in result.tool_calls] == ["delegate_to_specialist"]


def _sdk_stream_snapshot(partial_json_chunks, stop_reason):
    """Run the SDK's own streaming accumulator over a tool_use block whose input_json_delta was cut."""
    from anthropic.lib.streaming._messages import accumulate_event
    events = [
        {"type": "message_start", "message": {
            "id": "m1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "tu_cut", "name": "deliver_response", "input": {}}},
        *[{"type": "content_block_delta", "index": 0,
           "delta": {"type": "input_json_delta", "partial_json": c}} for c in partial_json_chunks],
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 16000, "input_tokens": 10, "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0}},
    ]
    snapshot = None
    for event in events:
        snapshot = accumulate_event(event=event, current_snapshot=snapshot)
    return snapshot


async def test_cut_input_json_stream_through_the_sdk_accumulator_is_dropped():
    """If jiter / the SDK ever surfaces an unfinished string as a partial value, this goes red."""
    snapshot = _sdk_stream_snapshot(['{"response": "{\\"text\\": \\"unfin', ''], "max_tokens")
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, snapshot)
    result = await adapter.generate_content(_req())
    assert result.tool_calls == []
    assert result.finish_reason == FinishReason.MAX_TOKENS


async def test_complete_input_json_stream_through_the_sdk_accumulator_is_kept():
    snapshot = _sdk_stream_snapshot(['{"response": "{\\"text\\": ', '\\"done\\"}"}'], "tool_use")
    adapter = ClaudeAdapter(api_key="k")
    _install(adapter, snapshot)
    result = await adapter.generate_content(_req())
    assert [(c.name, c.args) for c in result.tool_calls] == [("deliver_response", {"response": '{"text": "done"}'})]


# --------------------------------------------------------------------------- preserved thinking (R8)

def _delegation_setup():
    coordinator = MagicMock(spec=AgentCoordinator)
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
        task_id="t1", agent_id="memory", result="facts found"))
    return DelegationEngine(coordinator), coordinator


async def _run_loop(use_clock):
    adapter = ClaudeAdapter(api_key="k")
    calls = _install(
        adapter,
        _response(_tool_block("delegate_to_specialist", {"intent": "search_memory", "query": "q"}, tc_id="tu_1")),
        _response(_tool_block("deliver_response", {"response": _ANSWER}, tc_id="tu_2")),
    )
    engine, coordinator = _delegation_setup()
    base = _req(thinking="medium")

    async def call_llm(request, turn):
        return await adapter.generate_content(request)

    token = None
    kwargs = {}
    if use_clock:
        # Turn 2 is the last allowed turn (max_turns=2), so the engine makes it a wrap-up call:
        # it appends WRAP_UP_NOTE and clamps the timeout on a copy of the base request.
        token = CURRENT_TURN_CLOCK.set(TurnClock.start(budget_s=10_000, wrap_up_reserve_s=1))
        kwargs = {"use_turn_clock": True}
    try:
        result = await engine.execute(call_llm=call_llm, base_request=base, context={}, max_turns=2,
                                      terminal_tool="deliver_response", **kwargs)
    finally:
        if token is not None:
            CURRENT_TURN_CLOCK.reset(token)
    return result, calls, coordinator


async def test_tools_and_system_are_unchanged_between_turns_of_a_real_delegation_loop():
    result, calls, coordinator = await _run_loop(use_clock=False)
    assert len(calls) == 2
    assert calls[0]["tools"] == calls[1]["tools"] and calls[0]["tools"][-1] == _EXPECTED_DELIVER
    assert calls[0]["system"] == calls[1]["system"]
    assert calls[0]["messages"] != calls[1]["messages"]
    coordinator.handle_delegation.assert_awaited_once()
    assert result.terminal_tool_args == {"response": _ANSWER}


async def test_tools_and_system_are_unchanged_on_the_wrap_up_turn():
    result, calls, _ = await _run_loop(use_clock=True)
    assert len(calls) == 2
    assert calls[0]["tools"] == calls[1]["tools"]
    assert calls[0]["system"] == calls[1]["system"]
    assert calls[0]["thinking"] == calls[1]["thinking"]
    assert calls[0]["output_config"] == calls[1]["output_config"]
    last = calls[1]["messages"][-1]
    assert last["role"] == "user"
    assert WRAP_UP_NOTE in str(last["content"])
    assert WRAP_UP_NOTE not in str(calls[0]["messages"])
    assert result.terminal_tool_args == {"response": _ANSWER}
