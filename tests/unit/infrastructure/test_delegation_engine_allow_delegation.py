"""`allow_delegation=False` (DRIVE_SPECIALIST_RFC §5.4): an agent with local tools only declares no
delegate_to_specialist, so a stray call gets the existing unknown-tool answer and never reaches the coordinator."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DELEGATE_TOOL_NAME, DelegationEngine, ToolResult


def _base_request():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


@pytest.fixture
def coordinator():
    c = MagicMock(spec=AgentCoordinator)
    c.handle_delegation = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="x", result="ok"))
    return c


def _seen(call_llm):
    request = call_llm.await_args_list[1].args[0]
    return str([p.tool_response for m in request.messages for p in m.parts if p.tool_response][-1])


def _llm(call):
    return AsyncMock(side_effect=[LLMResponse(text="", tool_calls=[call]), LLMResponse(text="done", tool_calls=[])])


async def _local(tc):
    return ToolResult(name=tc.name, result_str="BODY")


async def test_a_stray_delegate_call_is_an_unknown_tool_without_delegation(coordinator):
    call_llm = _llm(ToolCall(name=DELEGATE_TOOL_NAME, args={"intent": "search_web", "query": "q"}))
    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        local_tools={"search_drive": _local}, terminal_tool="report_to_orchestrator", allow_delegation=False)
    coordinator.handle_delegation.assert_not_awaited()
    seen = _seen(call_llm)
    assert f"unknown tool '{DELEGATE_TOOL_NAME}'" in seen
    assert "Declared tools: report_to_orchestrator, search_drive." in seen


async def test_a_co_emitted_delegate_call_is_not_dispatched_either(coordinator):
    call_llm = AsyncMock(return_value=LLMResponse(text="", tool_calls=[
        ToolCall(name=DELEGATE_TOOL_NAME, args={"intent": "search_web", "query": "q"}),
        ToolCall(name="report_to_orchestrator", args={"note": "n"})]))
    result = await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_base_request(), context={}, max_turns=3,
        local_tools={"search_drive": _local}, terminal_tool="report_to_orchestrator", allow_delegation=False)
    coordinator.handle_delegation.assert_not_awaited()
    assert result.terminal_tool_args == {"note": "n"}


async def test_delegation_stays_declared_by_default(coordinator):
    call_llm = _llm(ToolCall(name=DELEGATE_TOOL_NAME, args={"intent": "search_web", "query": "q"}))
    await DelegationEngine(coordinator).execute(call_llm=call_llm, base_request=_base_request(), context={},
                                                max_turns=3)
    coordinator.handle_delegation.assert_awaited_once()
