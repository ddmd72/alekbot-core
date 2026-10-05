"""Final review I1 + I2: the wrap-up keeps its text; a tool batch is bounded by the clock."""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from src.domain.llm import MessagePart
from src.domain.turn_clock import CURRENT_TURN_CLOCK, WRAP_UP_NOTE, TurnClock
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult
from src.ports.llm_port import LLMRequest, LLMResponse, Message, ToolCall


def _tool(name="delegate_to_specialist", **args):
    return ToolCall(name=name, args=args or {"intent": "search_web", "query": "q"})


def _req():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


def _engine():
    engine = DelegationEngine(MagicMock())
    engine.dispatch = AsyncMock(side_effect=AssertionError("must not dispatch"))
    return engine


async def _run(engine, call_llm, clock, max_turns=5):
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        return await engine.execute(call_llm=call_llm, base_request=_req(), context={},
                                    max_turns=max_turns, terminal_tool="deliver_response",
                                    use_turn_clock=True)
    finally:
        CURRENT_TURN_CLOCK.reset(token)


def _in_reserve_clock():
    return TurnClock.start(budget_s=100, wrap_up_reserve_s=200)


# --- I1: wrap-up branches --------------------------------------------------------


async def test_wrap_up_with_text_and_tool_calls_returns_the_text_as_success(caplog):
    async def call_llm(req, turn):
        return LLMResponse(text="here is what I have", tool_calls=[_tool(), _tool(name="use_skill")])

    with caplog.at_level(logging.WARNING):
        result = await _run(_engine(), call_llm, _in_reserve_clock())
    assert result.failed is False
    assert result.text == "here is what I have"
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "delegate_to_specialist" in warning and "use_skill" in warning


async def test_wrap_up_terminal_call_logs_dropped_siblings(caplog):
    async def call_llm(req, turn):
        return LLMResponse(text="", tool_calls=[
            ToolCall(name="deliver_response", args={"text": "final"}), _tool()])

    with caplog.at_level(logging.WARNING):
        result = await _run(_engine(), call_llm, _in_reserve_clock())
    assert result.failed is False
    assert result.terminal_tool_args == {"text": "final"}
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "delegate_to_specialist" in warning


async def test_wrap_up_terminal_call_alone_logs_no_warning(caplog):
    async def call_llm(req, turn):
        return LLMResponse(text="", tool_calls=[ToolCall(name="deliver_response", args={"text": "f"})])

    with caplog.at_level(logging.WARNING):
        result = await _run(_engine(), call_llm, _in_reserve_clock())
    assert result.terminal_tool_args == {"text": "f"}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_wrap_up_with_neither_text_nor_terminal_call_fails_with_an_accurate_warning(caplog):
    async def call_llm(req, turn):
        return LLMResponse(text="", tool_calls=[_tool()])

    with caplog.at_level(logging.WARNING):
        result = await _run(_engine(), call_llm, _in_reserve_clock())
    assert result.failed is True
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "no answer" in warning and "delegate_to_specialist" in warning


async def test_wrap_up_with_empty_response_fails():
    async def call_llm(req, turn):
        return LLMResponse(text="")

    result = await _run(_engine(), call_llm, _in_reserve_clock())
    assert result.failed is True


# --- I2: a tool batch is bounded by the clock -------------------------------------


async def test_slow_tool_batch_gets_synthetic_results_and_the_wrap_up_still_happens():
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10)
    clock.call_timeout = lambda now=None: 0.05   # the batch may run 50 ms
    engine = DelegationEngine(MagicMock())

    async def slow_batch(**kw):
        await asyncio.sleep(5)
        return []
    engine._execute_tool_calls = slow_batch
    calls = []

    async def call_llm(req, turn):
        calls.append(req)
        if turn == 1:
            return LLMResponse(text="", tool_calls=[_tool(), _tool(name="use_skill", name_arg="x")])
        return LLMResponse(text="partial answer")

    result = await asyncio.wait_for(_run(engine, call_llm, clock), timeout=2)
    assert result.text == "partial answer" and result.failed is False
    assert len(calls) == 2
    wrap_up = calls[1]
    assert any(p.text == WRAP_UP_NOTE for p in wrap_up.messages[-1].parts)
    assert wrap_up.timeout == clock.wrap_up_timeout()
    # 1:1 call/result: one synthetic result per tool call, in the tool turn.
    responses = [p.tool_response for m in wrap_up.messages for p in m.parts if p.tool_response]
    assert len(responses) == 2
    assert all("not finished: turn budget ended" in str(r) for r in responses)


async def test_synthetic_results_are_marked_failed():
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10)
    clock.call_timeout = lambda now=None: 0.05
    engine = DelegationEngine(MagicMock())

    async def slow_batch(**kw):
        await asyncio.sleep(5)
        return []
    engine._execute_tool_calls = slow_batch
    seen = {}
    real_accumulate = engine._accumulate_tool_metadata

    def capture(tool_results, *a, **k):
        seen["results"] = list(tool_results)
        return real_accumulate(tool_results, *a, **k)
    engine._accumulate_tool_metadata = capture

    async def call_llm(req, turn):
        if turn == 1:
            return LLMResponse(text="", tool_calls=[_tool()])
        return LLMResponse(text="ok")

    await asyncio.wait_for(_run(engine, call_llm, clock), timeout=2)
    [tr] = seen["results"]
    assert isinstance(tr, ToolResult)
    assert tr.failed is True and tr.result_str == "not finished: turn budget ended"
    assert tr.name == "delegate_to_specialist"


async def test_fast_tool_batch_is_not_cut():
    clock = TurnClock.start(budget_s=10_000, wrap_up_reserve_s=10)
    engine = DelegationEngine(MagicMock())
    engine._execute_tool_calls = AsyncMock(return_value=[
        ToolResult(name="delegate_to_specialist", result_str="real result")])
    calls = []

    async def call_llm(req, turn):
        calls.append(req)
        if turn == 1:
            return LLMResponse(text="", tool_calls=[_tool()])
        return LLMResponse(text="final")

    result = await _run(engine, call_llm, clock)
    assert result.text == "final"
    # The second call is a normal turn, not a wrap-up.
    assert not any(p.text == WRAP_UP_NOTE for p in calls[1].messages[-1].parts)
    responses = [p.tool_response for m in calls[1].messages for p in m.parts if p.tool_response]
    assert "real result" in str(responses)
