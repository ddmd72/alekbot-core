from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.utils.telemetry as telem
from src.domain.agent import AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult


def _base_request():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


@pytest.fixture
def coordinator():
    c = MagicMock(spec=AgentCoordinator)
    c.handle_delegation = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="x", result="spec"))
    return c


def _handler(result_str="BODY", history=None):
    async def handle(tc: ToolCall) -> ToolResult:
        return ToolResult(name=tc.name, result_str=result_str, history_context=history)
    return AsyncMock(side_effect=handle)


async def test_local_tool_goes_to_handler_not_coordinator(coordinator):
    handler = _handler(history={"skill_context": {"name": "a", "version": 1, "body": "BODY"}})
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    result = await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        local_tools={"use_skill": handler},
    )

    handler.assert_awaited_once()
    coordinator.handle_delegation.assert_not_awaited()
    assert result.text == "done"
    assert result.history_contexts == {"skill_context": [{"name": "a", "version": 1, "body": "BODY"}]}
    second_request = call_llm.await_args_list[1].args[0]
    tool_parts = [p for m in second_request.messages for p in m.parts if p.tool_response]
    assert tool_parts and "BODY" in str(tool_parts[-1].tool_response)


async def test_mixed_batch_dispatches_both_kinds(coordinator):
    handler = _handler()
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[
            ToolCall(name="use_skill", args={"name": "a"}),
            ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "q"}),
        ]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        local_tools={"use_skill": handler},
    )

    handler.assert_awaited_once()
    coordinator.handle_delegation.assert_awaited_once()


async def test_handler_exception_becomes_failed_tool_result(coordinator):
    async def boom(tc):
        raise RuntimeError("store down")
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    result = await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        local_tools={"use_skill": boom},
    )

    assert result.text == "done"
    second_request = call_llm.await_args_list[1].args[0]
    assert "store down" in str(second_request.messages[-1].parts[-1].tool_response)


async def test_terminal_sibling_local_tool_contributes_no_history(coordinator):
    handler = _handler(history={"skill_context": {"name": "a", "version": 1, "body": "BODY"}})
    call_llm = AsyncMock(return_value=LLMResponse(text="", tool_calls=[
        ToolCall(name="use_skill", args={"name": "a"}),
        ToolCall(name="deliver_response", args={"full_response": "answer"}),
    ]))

    result = await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        terminal_tool="deliver_response", local_tools={"use_skill": handler},
    )

    handler.assert_awaited_once()
    assert result.terminal_tool_args == {"full_response": "answer"}
    assert result.history_contexts is None


@pytest.fixture
def span_exporter(monkeypatch):
    # Same pattern as tests/unit/infrastructure/test_delegation_spans.py.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telem, "_tracer", provider.get_tracer("test"))
    return exporter


async def test_local_call_emits_its_own_span(coordinator, span_exporter):
    handler = _handler()
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])
    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        local_tools={"use_skill": handler},
    )
    spans = [s for s in span_exporter.get_finished_spans() if s.name == "delegation.local_tool"]
    assert len(spans) == 1
    assert spans[0].attributes["delegation.tool_name"] == "use_skill"
    assert spans[0].attributes["delegation.tool_arg_name"] == "a"


async def test_without_local_tools_behaviour_is_unchanged(coordinator):
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])
    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
    )
    # No map → treated as a delegation, which fails cleanly for lack of `intent`.
    second_request = call_llm.await_args_list[1].args[0]
    assert "without 'intent'" in str(second_request.messages[-1].parts[-1].tool_response)
