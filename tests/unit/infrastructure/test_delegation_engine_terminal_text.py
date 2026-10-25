"""Text the model writes beside the terminal call travels in DelegationResult.text (R5)."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.domain.turn_clock import CURRENT_TURN_CLOCK, TurnClock
from src.infrastructure.delegation_engine import DelegationEngine

TERMINAL = "deliver_response"
ARGS = {"response": '{"full_response": "hi"}'}


def _req() -> LLMRequest:
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


async def _run(text: str):
    engine = DelegationEngine(coordinator=MagicMock())
    call_llm = AsyncMock(return_value=LLMResponse(
        text=text, tool_calls=[ToolCall(name=TERMINAL, args=dict(ARGS))]))
    result = await engine.execute(
        call_llm=call_llm, base_request=_req(), context={}, max_turns=3,
        terminal_tool=TERMINAL, calling_agent_id="smart_u1",
    )
    assert call_llm.await_count == 1
    return result


async def test_text_beside_terminal_call_is_carried():
    result = await _run("intro")
    assert result.text == "intro"
    assert result.terminal_tool_args == ARGS


async def test_no_text_beside_terminal_call_is_empty_string():
    result = await _run("")
    assert result.text == ""
    assert result.terminal_tool_args == ARGS


async def test_none_text_becomes_empty_string():
    engine = DelegationEngine(coordinator=MagicMock())
    resp = LLMResponse(text="", tool_calls=[ToolCall(name=TERMINAL, args=dict(ARGS))])
    resp.text = None
    result = await engine.execute(
        call_llm=AsyncMock(return_value=resp), base_request=_req(), context={},
        max_turns=3, terminal_tool=TERMINAL,
    )
    assert result.text == ""


async def test_terminal_turn_is_not_appended_to_messages():
    result = await _run("intro")
    # only the original user message: no assistant tool_use turn left dangling
    assert [m.role for m in result.messages] == ["user"]


async def test_wrap_up_terminal_carries_text():
    clock = TurnClock.start(budget_s=100, wrap_up_reserve_s=200)  # already in reserve
    engine = DelegationEngine(coordinator=MagicMock())
    call_llm = AsyncMock(return_value=LLMResponse(
        text="wrapping", tool_calls=[ToolCall(name=TERMINAL, args=dict(ARGS))]))
    token = CURRENT_TURN_CLOCK.set(clock)
    try:
        result = await engine.execute(
            call_llm=call_llm, base_request=_req(), context={}, max_turns=3,
            terminal_tool=TERMINAL, use_turn_clock=True,
        )
    finally:
        CURRENT_TURN_CLOCK.reset(token)
    assert result.text == "wrapping"
    assert result.terminal_tool_args == ARGS
    assert [m.role for m in result.messages] == ["user"]
