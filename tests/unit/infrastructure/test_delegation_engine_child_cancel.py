"""P15-S5 beyond the two pins (owner decision O4): a gathered child's own CancelledError is that child's failure, while a
cancel of the turn itself, and any other BaseException, still propagates.

The pins (a child of the tool batch and a fan-out secondary that raise CancelledError) live in
`test_delegation_engine_gather_interleaving.py`.
"""
import asyncio

import pytest

from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.delegation_engine import DELEGATE_TOOL_NAME, DelegationEngine, ToolResult, _gathered_failure


def _delegate(intent: str) -> ToolCall:
    return ToolCall(name=DELEGATE_TOOL_NAME, args={"intent": intent, "query": "q"})


async def _batch(engine: DelegationEngine, monkeypatch, raise_in_second: BaseException):
    async def dispatch(tool_call, *args, **kwargs) -> ToolResult:
        if tool_call.args["intent"] == "second":
            raise raise_in_second
        return ToolResult(name=tool_call.name, result_str="first answer")

    monkeypatch.setattr(engine, "dispatch", dispatch)
    return await engine._execute_tool_calls(
        tool_calls=[_delegate("first"), _delegate("second")], context={"user_id": "u1"}, intent_remap={},
        intent_fanout={}, calling_agent_id="smart_agent_u1", max_retries=1, retry_backoff=0,
    )


class TestGatheredFailureText:

    async def test_a_childs_own_cancel_is_named_as_a_cancelled_specialist(self):
        assert _gathered_failure(asyncio.CancelledError()) == "the specialist was cancelled"

    async def test_an_exception_keeps_its_own_text(self):
        assert _gathered_failure(ValueError("quota exceeded")) == "quota exceeded"

    async def test_a_cancel_of_the_running_task_propagates(self):
        # The raise is observed inside the task: a task that cancelled itself ends cancelled even when the coroutine
        # returns normally, so awaiting the task would not tell a propagated cancel from a swallowed one.
        async def turn():
            asyncio.current_task().cancel()  # the turn is being cancelled; no await has delivered it yet
            try:
                _gathered_failure(asyncio.CancelledError())
            except asyncio.CancelledError:
                asyncio.current_task().uncancel()
                return "propagated"
            asyncio.current_task().uncancel()
            return "converted"

        assert await asyncio.create_task(turn()) == "propagated"

    async def test_a_keyboard_interrupt_propagates(self):
        with pytest.raises(KeyboardInterrupt):
            _gathered_failure(KeyboardInterrupt())


class TestBatch:

    async def test_the_failed_tool_result_says_the_specialist_was_cancelled(self, monkeypatch):
        results = await _batch(DelegationEngine(AgentCoordinator()), monkeypatch, asyncio.CancelledError())

        assert [r.failed for r in results] == [False, True]
        assert results[1].result_str == "AGENT ERROR: the specialist was cancelled"


class TestFanoutMerge:

    def _merge(self, primary, secondary) -> ToolResult:
        engine = DelegationEngine(AgentCoordinator())
        return engine._merge_fanout_results(_delegate("search_web"), ["search_web", "maps_query"], [primary, secondary])

    async def test_a_cancelled_primary_is_a_failed_section_and_the_secondary_answer_stays(self):
        secondary = AgentResponse.success(task_id="t", agent_id="maps_search_agent_u1", result="maps answer")

        merged = self._merge(asyncio.CancelledError(), secondary)

        assert merged.failed
        assert "AGENT ERROR: the specialist was cancelled" in merged.result_str
        assert "maps answer" in merged.result_str
