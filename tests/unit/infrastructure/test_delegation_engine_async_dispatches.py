"""DelegationEngine records successfully dispatched ASYNC intents (CDR T4)."""
from typing import Dict

from src.domain.agent import AgentResponse
from src.domain.llm import MessagePart
from src.infrastructure.agent_registry import ExecutionMode
from src.infrastructure.delegation_engine import DelegationEngine
from src.ports.llm_port import LLMRequest, LLMResponse, Message, ToolCall


class FakeCoordinator:
    """Answers per intent: a mode table and a set of intents that fail."""

    def __init__(self, modes: Dict[str, ExecutionMode], failing=()):
        self._modes = modes
        self._failing = set(failing)

    def get_execution_mode(self, intent):
        return self._modes.get(intent)

    async def handle_delegation(self, intent, query, context, calling_agent_id):
        if intent in self._failing:
            return AgentResponse.failure(task_id="t", agent_id="a", error="boom")
        return AgentResponse.success(task_id="t", agent_id="a", result="ok")


def _delegate(intent):
    return ToolCall(name="delegate_to_specialist", args={"intent": intent, "query": "q"})


async def _run(coordinator, calls, remap=None):
    turns = [LLMResponse(text="", tool_calls=calls), LLMResponse(text="", tool_calls=[
        ToolCall(name="deliver_response", args={"response": ""})])]

    async def call_llm(req, turn=0):
        return turns.pop(0)

    return await DelegationEngine(coordinator).execute(
        call_llm=call_llm,
        base_request=LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])]),
        context={}, max_turns=4, terminal_tool="deliver_response",
        intent_remap=remap or {},
    )


MODES = {"create_html_page": ExecutionMode.ASYNC, "search_web": ExecutionMode.SYNC,
         "make_page": ExecutionMode.ASYNC}


async def test_successful_async_dispatch_is_recorded():
    r = await _run(FakeCoordinator(MODES), [_delegate("create_html_page")])
    assert r.async_dispatches == ["create_html_page"]


async def test_sync_dispatch_is_not_recorded():
    r = await _run(FakeCoordinator(MODES), [_delegate("search_web")])
    assert r.async_dispatches == []


async def test_failed_async_dispatch_is_not_recorded():
    r = await _run(FakeCoordinator(MODES, failing={"create_html_page"}), [_delegate("create_html_page")])
    assert r.async_dispatches == []


async def test_remapped_intent_is_recorded_under_its_target_name():
    r = await _run(FakeCoordinator(MODES), [_delegate("alias")], remap={"alias": "make_page"})
    assert r.async_dispatches == ["make_page"]


async def test_async_dispatch_co_emitted_with_the_terminal_call_is_recorded():
    async def call_llm(req, turn=0):
        return LLMResponse(text="", tool_calls=[
            _delegate("create_html_page"), ToolCall(name="deliver_response", args={"response": ""})])

    r = await DelegationEngine(FakeCoordinator(MODES)).execute(
        call_llm=call_llm,
        base_request=LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])]),
        context={}, max_turns=3, terminal_tool="deliver_response",
    )
    assert r.async_dispatches == ["create_html_page"]
