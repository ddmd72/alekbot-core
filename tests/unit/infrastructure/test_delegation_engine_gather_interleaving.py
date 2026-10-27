"""P15 · J1: the delegation engine's gathered tool batch and fan-out, and the model's ToolCall it dispatches.

`DelegationEngine._execute_tool_calls` and `_dispatch_with_fanout` gather their children with `return_exceptions=True`
and then sort the results with `isinstance(result, Exception)`. A child that raises `CancelledError` on its own (a
`BaseException`, not an `Exception`, and not a cancel of the batch) slips past that check. The concurrent cases are forced
with `asyncio.Event` gates: one child holds until the other has started, and the test asserts the gate was reached.

The fan-out and file-ref cases run through a real `AgentCoordinator` over the real manifest registry with stub specialists
registered under their production ids; only the specialists' answers are scripted.
"""
import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from src.domain.agent import AgentResponse
from src.domain.llm import ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, SEARCH_WEB_MAPS_FANOUT, Intent
from src.infrastructure.agent_registry import AgentRegistry
from src.infrastructure.delegation_engine import DELEGATE_TOOL_NAME, DelegationEngine, ToolResult

USER = "u1"
CALLER = f"smart_agent_{USER}"
GATE_TIMEOUT_S = 2.0


class _Specialist:
    """A registered specialist: `answer` is awaited for each message (it may gate or raise); messages are kept."""

    def __init__(self, base_id: str, answer) -> None:
        self.agent_id = f"{base_id}_{USER}"
        self.agent_type = base_id
        self.config = SimpleNamespace(capabilities=[])
        self.answer = answer
        self.messages: List[Any] = []

    async def process(self, message) -> AgentResponse:
        self.messages.append(message)
        result = await self.answer(message)
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result=result)


def _coordinator(*specialists: _Specialist, file_ref_resolver=None) -> AgentCoordinator:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry, file_ref_resolver=file_ref_resolver)
    for specialist in specialists:
        coordinator.register_agent(specialist)
    return coordinator


def _delegate(intent: str, query: str = "q", context: Optional[Dict[str, Any]] = None) -> ToolCall:
    args: Dict[str, Any] = {"intent": intent, "query": query}
    if context is not None:
        args["context"] = context
    return ToolCall(name=DELEGATE_TOOL_NAME, args=args)


class TestGatheredBatch:

    async def _batch(self, engine: DelegationEngine, monkeypatch, second_raises: bool):
        started = asyncio.Event()

        async def dispatch(tool_call, *args, **kwargs) -> ToolResult:
            if tool_call.args["intent"] == "first":
                started.set()
                await asyncio.sleep(0)
                return ToolResult(name=tool_call.name, result_str="first answer")
            await asyncio.wait_for(started.wait(), GATE_TIMEOUT_S)  # both children are in flight
            if second_raises:
                raise asyncio.CancelledError()
            return ToolResult(name=tool_call.name, result_str="second answer")

        monkeypatch.setattr(engine, "dispatch", dispatch)
        results = await engine._execute_tool_calls(
            tool_calls=[_delegate("first"), _delegate("second")],
            context={"user_id": USER}, intent_remap={}, intent_fanout={}, calling_agent_id=CALLER,
            max_retries=1, retry_backoff=0,
        )
        return started, results

    async def test_control_two_concurrent_calls_give_two_tool_results(self, monkeypatch):
        started, results = await self._batch(DelegationEngine(AgentCoordinator()), monkeypatch, second_raises=False)

        assert started.is_set()
        assert [r.result_str for r in results] == ["first answer", "second answer"]

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S5: a tool call that raises CancelledError on its own passes isinstance(result, Exception) and is "
        "returned as the batch's result instead of a failed ToolResult; owner decision"))
    async def test_a_child_that_raises_cancelled_error_becomes_a_failed_tool_result(self, monkeypatch):
        """`_execute_tool_calls` (delegation_engine.py:640-690) gathers with `return_exceptions=True` and converts only
        `isinstance(result, Exception)` (:680); a `CancelledError` (a `BaseException`) is appended as the result."""
        started = None
        results: List[Any] = []
        try:
            started, results = await self._batch(DelegationEngine(AgentCoordinator()), monkeypatch, second_raises=True)
        except BaseException:  # the batch itself must not raise either
            results = []

        assert started is not None and started.is_set()
        assert len(results) == 2
        assert isinstance(results[0], ToolResult) and not results[0].failed
        assert isinstance(results[1], ToolResult) and results[1].failed


class TestFanout:

    async def _fanout(self, secondary_raises: bool):
        web_started = asyncio.Event()

        async def web(message) -> str:
            web_started.set()
            await asyncio.sleep(0)
            return "web answer about the weather in Springfield"

        async def maps(message) -> str:
            await asyncio.wait_for(web_started.wait(), GATE_TIMEOUT_S)  # primary and secondary are in flight
            if secondary_raises:
                raise asyncio.CancelledError()
            return "maps answer"

        coordinator = _coordinator(_Specialist("web_search_agent", web), _Specialist("maps_search_agent", maps))
        engine = DelegationEngine(coordinator)
        merged: Optional[ToolResult] = None
        raised: Optional[BaseException] = None
        try:
            merged = await engine.dispatch(
                _delegate(Intent.SEARCH_WEB, "the weather in Springfield today"), {"user_id": USER},
                intent_remap={}, intent_fanout={Intent.SEARCH_WEB: SEARCH_WEB_MAPS_FANOUT}, calling_agent_id=CALLER,
            )
        except BaseException as exc:  # an exception from the merge is a wrong answer, reported by the assertions
            raised = exc
        return web_started, merged, raised

    async def test_control_primary_and_secondary_are_merged(self):
        web_started, merged, raised = await self._fanout(secondary_raises=False)

        assert web_started.is_set()
        assert raised is None and merged is not None and not merged.failed
        assert "web answer about the weather in Springfield" in merged.result_str and "maps answer" in merged.result_str

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S5: a fan-out secondary that raises CancelledError reaches _merge_fanout_results as a non-Exception, "
        "whose response.status raises and wipes the primary's result; owner decision"))
    async def test_a_secondary_that_raises_cancelled_error_keeps_the_primary_answer(self):
        """`_dispatch_with_fanout` (delegation_engine.py:833-862) gathers primary + secondaries with
        `return_exceptions=True`; `_merge_fanout_results` (:864-935) skips only `isinstance(response, Exception)` and
        reads `response.status` on the `CancelledError` (the loop at :886, the read at :899), so the whole dispatch raises."""
        web_started, merged, raised = await self._fanout(secondary_raises=True)

        assert web_started.is_set()
        assert merged is not None, f"the fan-out dispatch raised {raised!r}"
        assert "web answer about the weather in Springfield" in merged.result_str
        assert not merged.failed


class TestFileRefResolution:

    async def _dispatch_with_a_file_ref(self):
        async def resolve(ref: str, user_id: str) -> str:
            return "BIG TEXT"

        async def memory(message) -> str:
            return "facts"

        specialist = _Specialist("facts_memory_agent", memory)
        engine = DelegationEngine(_coordinator(specialist, file_ref_resolver=resolve))
        tool_call = _delegate(Intent.SEARCH_MEMORY, "what does the lease say", context={"file_ref": "f1"})
        result = await engine.dispatch(tool_call, {"user_id": USER}, intent_remap={}, intent_fanout={},
                                       calling_agent_id=CALLER)
        return tool_call, specialist, result

    async def test_control_the_specialist_receives_the_resolved_file_text(self):
        _, specialist, result = await self._dispatch_with_a_file_ref()

        assert not result.failed
        assert result.result_str == "facts"
        assert specialist.messages[0].payload["file_content"] == "BIG TEXT"
        assert specialist.messages[0].payload["file_ref"] == "f1"

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S6: the coordinator writes the resolved file text into the model's own ToolCall context dict, which "
        "build_tool_turn echoes into history; owner decision"))
    async def test_the_resolved_file_text_does_not_enter_the_models_tool_call(self):
        """`normalize_delegate_context` returns the model's own dict (delegation_engine.py:130-134), the engine passes it
        on as `params`, and `AgentCoordinator._resolve_file_refs` writes `params["file_content"]` into it
        (agent_coordinator.py:590-606). That dict is `tool_call.args["context"]`, the call `build_tool_turn` puts into
        history: the whole file text rides along in every later turn."""
        tool_call, specialist, result = await self._dispatch_with_a_file_ref()

        assert not result.failed
        assert specialist.messages[0].payload["file_content"] == "BIG TEXT"  # the specialist gets the text
        assert "file_content" not in tool_call.args["context"]
