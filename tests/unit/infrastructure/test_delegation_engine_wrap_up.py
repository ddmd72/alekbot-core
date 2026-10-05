"""Wrap-up instead of a hard stop (RFC §5.2, plan delta D3)."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock, WRAP_UP_NOTE
from src.infrastructure.delegation_engine import DelegationEngine
from src.ports.llm_port import LLMRequest, LLMResponse, Message, ToolCall
from src.domain.llm import MessagePart


def _tool(name="delegate_to_specialist", **args):
    return ToolCall(name=name, args=args or {"intent": "search_web", "query": "q"})


def _engine():
    coordinator = MagicMock()
    engine = DelegationEngine(coordinator)
    engine.dispatch = AsyncMock(side_effect=AssertionError("must not dispatch"))
    return engine


def _req():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


async def _run(engine, call_llm, clock, **kw):
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        return await engine.execute(call_llm=call_llm, base_request=_req(), context={},
                                    max_turns=kw.pop("max_turns", 5),
                                    terminal_tool="deliver_response", **kw)
    finally:
        CURRENT_TURN_CLOCK.reset(token)


async def test_in_reserve_the_first_call_is_a_wrap_up_with_the_note():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=200)  # already in reserve
    seen = []

    async def call_llm(req, turn):
        seen.append(req)
        return LLMResponse(text="", tool_calls=[ToolCall(name="deliver_response", args={"text": "partial"})])

    result = await _run(_engine(), call_llm, clock)
    assert result.terminal_tool_args == {"text": "partial"}
    last = seen[0].messages[-1]
    assert last.role == "user" and any(p.text == WRAP_UP_NOTE for p in last.parts)
    assert seen[0].timeout == clock.wrap_up_timeout()


async def test_wrap_up_does_not_dispatch_other_tools():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=200)

    async def call_llm(req, turn):
        return LLMResponse(text="", tool_calls=[_tool()])

    result = await _run(_engine(), call_llm, clock)   # engine.dispatch raises if called
    assert result.failed is True


async def test_last_allowed_turn_is_a_wrap_up():
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10)
    engine = _engine()
    engine.dispatch = AsyncMock(return_value=MagicMock(name="ToolResult"))
    calls = []

    async def call_llm(req, turn):
        calls.append(req)
        if turn < 2:
            return LLMResponse(text="", tool_calls=[_tool()])
        return LLMResponse(text="done so far")

    engine._execute_tool_calls = AsyncMock(return_value=[
        MagicMock(name="tr", result_str="r", file_data=None, structured_data=None,
                  history_context=None, delivery_items=[])
    ])
    result = await _run(engine, call_llm, clock, max_turns=2)
    assert result.text == "done so far"
    assert any(p.text == WRAP_UP_NOTE for p in calls[1].messages[-1].parts)


async def test_without_a_clock_nothing_changes():
    async def call_llm(req, turn):
        return LLMResponse(text="plain")

    engine = DelegationEngine(MagicMock())
    result = await engine.execute(call_llm=call_llm, base_request=_req(), context={},
                                  max_turns=3, terminal_tool="deliver_response")
    assert result.text == "plain"


async def test_step_is_tracked():
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10)
    steps = []

    async def call_llm(req, turn):
        steps.append(clock.step)
        return LLMResponse(text="x")

    await _run(DelegationEngine(MagicMock()), call_llm, clock)
    assert steps == ["thinking"]
