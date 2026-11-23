"""DelegationEngine `observe`: each tool batch is observed after it ran and before the model's next call
(EXTERNAL_TASK_QUEUE_RFC §5.3); the siblings of a terminal call, which the model never reads, are not."""
from typing import List
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult


def _base():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


def _coordinator():
    c = MagicMock(spec=AgentCoordinator)
    c.handle_delegation = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="x", result="spec"))
    return c


def _local(result: str):
    async def handle(tc: ToolCall) -> ToolResult:
        return ToolResult(name=tc.name, result_str=result)
    return handle


async def test_each_batch_is_observed_before_the_next_model_call():
    events: List[str] = []

    async def call_llm(request, turn=0):
        events.append(f"model{turn}")
        return [
            LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"})]),
            LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "b"}),
                                             ToolCall(name="use_skill", args={"name": "c"})]),
            LLMResponse(text="done", tool_calls=[]),
        ][turn - 1]

    def observe(calls, results):
        events.append(f"observed:{[c.args['name'] for c in calls]}:{[r.result_str for r in results]}")

    await DelegationEngine(_coordinator()).execute(
        call_llm=call_llm, base_request=_base(), context={}, max_turns=5,
        local_tools={"use_skill": _local("BODY")}, observe=observe)

    assert events == ["model1", "observed:['a']:['BODY']", "model2", "observed:['b', 'c']:['BODY', 'BODY']", "model3"]


async def test_a_specialist_delegation_is_observed_with_its_intent():
    seen = []
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="delegate_to_specialist",
                                                  args={"intent": "search_web", "query": "q"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])

    await DelegationEngine(_coordinator()).execute(
        call_llm=call_llm, base_request=_base(), context={}, max_turns=3,
        observe=lambda calls, results: seen.append(([c.args.get("intent") for c in calls], len(results))))

    assert seen == [(["search_web"], 1)]


async def test_the_siblings_of_a_terminal_call_are_not_observed():
    seen = []
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"}),
                                         ToolCall(name="deliver_response", args={"text": "answer"})]),
    ])

    result = await DelegationEngine(_coordinator()).execute(
        call_llm=call_llm, base_request=_base(), context={}, max_turns=3, terminal_tool="deliver_response",
        local_tools={"use_skill": _local("BODY")}, observe=lambda calls, results: seen.append(calls))

    assert result.terminal_tool_args == {"text": "answer"}
    assert seen == []


async def test_no_observer_changes_nothing():
    call_llm = AsyncMock(side_effect=[
        LLMResponse(text="", tool_calls=[ToolCall(name="use_skill", args={"name": "a"})]),
        LLMResponse(text="done", tool_calls=[]),
    ])
    result = await DelegationEngine(_coordinator()).execute(
        call_llm=call_llm, base_request=_base(), context={}, max_turns=3, local_tools={"use_skill": _local("BODY")})
    assert result.text == "done"


async def test_chat_messages_picked_up_by_a_long_turn_are_observed_before_the_model_reads_them():
    from unittest.mock import AsyncMock as AM

    from src.domain.turn_clock import CURRENT_TURN_CLOCK, MEANWHILE_HEADER, TurnClock

    flagged = Message(role="model", parts=[MessagePart(text="found the kits online")], created_at=50.0,
                      execution_tainted=True)
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10, fetch_since=AM(side_effect=[[flagged], []]))
    clock.marked, clock.snapshot_at = True, 10.0
    events: List[str] = []

    async def call_llm(request, turn=0):
        events.append("model:" + str(any(MEANWHILE_HEADER in (p.text or "") for p in request.messages[-1].parts)))
        return LLMResponse(text="x")

    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        await DelegationEngine(_coordinator()).execute(
            call_llm=call_llm, base_request=_base(), context={}, max_turns=3, terminal_tool="deliver_response",
            use_turn_clock=True, observe_chat=lambda messages: events.append(f"chat:{[m.execution_tainted for m in messages]}"))
    finally:
        CURRENT_TURN_CLOCK.reset(token)

    assert events == ["chat:[True]", "model:True"]
