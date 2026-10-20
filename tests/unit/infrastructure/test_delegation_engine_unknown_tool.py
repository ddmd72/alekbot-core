import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult


def _base_request():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


@pytest.fixture
def coordinator():
    c = MagicMock(spec=AgentCoordinator)
    c.handle_delegation = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="x", result="ok"))
    return c


def _tool_result_text(request):
    parts = [p for m in request.messages for p in m.parts if p.tool_response]
    return str(parts[-1].tool_response)


async def test_unknown_tool_name_gets_an_error_naming_it_and_the_declared_tools(coordinator, caplog):
    async def handle(tc):
        return ToolResult(name=tc.name, result_str="BODY")
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="web_search", args={"query": "q"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    with caplog.at_level(logging.WARNING):
        result = await DelegationEngine(coordinator).execute(
            call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
            local_tools={"use_skill": AsyncMock(side_effect=handle)}, terminal_tool="deliver_response",
        )

    coordinator.handle_delegation.assert_not_awaited()
    assert result.text == "done"
    seen = _tool_result_text(call_llm.await_args_list[1].args[0])
    assert "unknown tool 'web_search'" in seen
    for declared in ("delegate_to_specialist", "use_skill", "deliver_response"):
        assert declared in seen
    assert "without 'intent'" not in seen
    assert any("web_search" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


async def test_delegate_without_intent_keeps_its_own_message(coordinator):
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="delegate_to_specialist", args={"query": "q"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
    )

    seen = _tool_result_text(call_llm.await_args_list[1].args[0])
    assert "delegate_to_specialist called without 'intent'" in seen
    assert "unknown tool" not in seen


async def test_unknown_tool_does_not_block_a_declared_one_in_the_same_batch(coordinator):
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[
            ToolCall(name="bogus", args={}),
            ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "q"}),
        ]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
    )

    coordinator.handle_delegation.assert_awaited_once()
