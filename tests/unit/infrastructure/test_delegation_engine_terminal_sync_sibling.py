"""A SYNC delegation co-emitted with the terminal tool logs a named WARN; dispatch is unchanged (R6)."""
import logging
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import ExecutionMode
from src.infrastructure.delegation_engine import DelegationEngine, ToolResult

TERMINAL = "deliver_response"
MODES = {"search_web": ExecutionMode.SYNC, "create_html_page": ExecutionMode.ASYNC}


def _delegate(intent: str) -> ToolCall:
    return ToolCall(name="delegate_to_specialist", args={"intent": intent, "query": "q"})


def _terminal() -> ToolCall:
    return ToolCall(name=TERMINAL, args={"response": "{}"})


class _Coordinator:
    """Coordinator whose mode lookup records every intent asked about."""

    def __init__(self, raises: bool = False):
        self.lookups: List[str] = []
        self.raises = raises
        self.mock = MagicMock(spec=AgentCoordinator)
        self.mock.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t", agent_id="a", result="ok"))
        self.mock.get_execution_mode = MagicMock(side_effect=self._mode)

    def _mode(self, intent: str) -> Optional[ExecutionMode]:
        self.lookups.append(intent)
        if self.raises:
            raise RuntimeError("registry down")
        return MODES.get(intent)

    def dispatched(self) -> List[str]:
        return [c.kwargs["intent"] for c in self.mock.handle_delegation.await_args_list]


async def _run(coord: _Coordinator, calls: List[ToolCall], remap: Optional[Dict[str, str]] = None,
               local=None):
    engine = DelegationEngine(coordinator=coord.mock)
    call_llm = AsyncMock(return_value=LLMResponse(text="", tool_calls=calls))
    req = LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])
    return await engine.execute(
        call_llm=call_llm, base_request=req, context={"user_id": "u1"}, max_turns=3,
        terminal_tool=TERMINAL, calling_agent_id="smart_u1", intent_remap=remap,
        local_tools=local,
    )


def _warns(caplog):
    return [r for r in caplog.records
            if getattr(r, "event", None) == "terminal_sync_sibling"]


async def test_sync_sibling_warns_and_is_still_dispatched(caplog):
    coord = _Coordinator()
    with caplog.at_level(logging.WARNING):
        result = await _run(coord, [_delegate("search_web"), _terminal()])
    warns = _warns(caplog)
    assert len(warns) == 1
    assert warns[0].levelno == logging.WARNING
    assert warns[0].intents == ["search_web"]
    assert warns[0].calling_agent_id == "smart_u1"
    assert coord.dispatched() == ["search_web"]
    assert result.terminal_tool_args == {"response": "{}"}


async def test_async_only_sibling_does_not_warn(caplog):
    coord = _Coordinator()
    with caplog.at_level(logging.WARNING):
        await _run(coord, [_delegate("create_html_page"), _terminal()])
    assert _warns(caplog) == []
    assert coord.dispatched() == ["create_html_page"]


async def test_mixed_siblings_warn_lists_only_sync(caplog):
    coord = _Coordinator()
    with caplog.at_level(logging.WARNING):
        await _run(coord, [_delegate("create_html_page"), _delegate("search_web"), _terminal()])
    warns = _warns(caplog)
    assert len(warns) == 1 and warns[0].intents == ["search_web"]
    assert sorted(coord.dispatched()) == ["create_html_page", "search_web"]


async def test_unknown_intent_does_not_warn(caplog):
    coord = _Coordinator()
    with caplog.at_level(logging.WARNING):
        await _run(coord, [_delegate("no_such_intent"), _terminal()])
    assert _warns(caplog) == []


async def test_remap_is_applied_before_the_lookup(caplog):
    coord = _Coordinator()
    with caplog.at_level(logging.WARNING):
        await _run(coord, [_delegate("a"), _terminal()], remap={"a": "search_web"})
    warns = _warns(caplog)
    assert len(warns) == 1 and warns[0].intents == ["search_web"]
    assert coord.lookups == ["search_web"]


async def test_local_tool_sibling_is_not_looked_up(caplog):
    coord = _Coordinator()
    handler = AsyncMock(side_effect=lambda tc: ToolResult(name=tc.name, result_str="ok"))
    with caplog.at_level(logging.WARNING):
        await _run(coord, [ToolCall(name="use_skill", args={"name": "x"}), _terminal()],
                   local={"use_skill": handler})
    assert coord.lookups == []
    assert _warns(caplog) == []
    handler.assert_awaited_once()


async def test_terminal_alone_does_no_lookup(caplog):
    coord = _Coordinator()
    await _run(coord, [_terminal()])
    assert coord.lookups == []


async def test_failing_lookup_does_not_break_the_turn(caplog):
    coord = _Coordinator(raises=True)
    with caplog.at_level(logging.WARNING):
        result = await _run(coord, [_delegate("search_web"), _terminal()])
    assert result.terminal_tool_args == {"response": "{}"}
    assert coord.dispatched() == ["search_web"]
    assert _warns(caplog) == []
    failures = [r for r in caplog.records
                if r.levelno == logging.WARNING and "lookup failed" in r.getMessage()]
    assert len(failures) == 1
