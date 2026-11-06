"""What the model reads back from the engine's own code-written results: `Tool error:` / `Tool note:`, never `SYSTEM`."""
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import SEARCH_WEB_MAPS_FANOUT, Intent
from src.infrastructure.delegation_engine import DelegationEngine
from tests.support.scripted_llm import calls, script


def _request():
    return LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])])


def _coordinator(response):
    c = MagicMock(spec=AgentCoordinator)
    c.handle_delegation = AsyncMock(return_value=response)
    return c


def _last_tool_result(seen):
    parts = [p for m in seen[-1].messages for p in m.parts if p.tool_response]
    return str(parts[-1].tool_response)


async def _run(coordinator, first_turn, **kwargs):
    call_llm, seen = script(first_turn, LLMResponse(text="done", tool_calls=[]))
    await DelegationEngine(coordinator).execute(
        call_llm=call_llm, base_request=_request(), context={}, max_turns=3, **kwargs,
    )
    return _last_tool_result(seen)


def _rejection():
    return AgentResponse.failure(task_id="t", agent_id="x", error="query is empty")


async def test_unknown_tool_is_a_tool_error():
    seen = await _run(_coordinator(_rejection()), calls(("web_search", {"query": "q"})))
    assert "Tool error: unknown tool 'web_search'" in seen
    assert "SYSTEM" not in seen


async def test_missing_intent_is_a_tool_error():
    seen = await _run(_coordinator(_rejection()), calls(("delegate_to_specialist", {"query": "q"})))
    assert "Tool error: delegate_to_specialist called without 'intent'" in seen
    assert "SYSTEM" not in seen


async def test_specialist_rejection_is_a_tool_note():
    seen = await _run(
        _coordinator(_rejection()), calls(("delegate_to_specialist", {"intent": "search_web", "query": "q"})),
    )
    assert "Tool note: Specialist agent rejected the request." in seen
    assert "SYSTEM" not in seen


async def test_fanout_preamble_and_primary_rejection_are_tool_notes():
    seen = await _run(
        _coordinator(_rejection()), calls(("delegate_to_specialist", {"intent": "search_web", "query": "q"})),
        intent_fanout={Intent.SEARCH_WEB: SEARCH_WEB_MAPS_FANOUT},
    )
    assert "Tool note: This query was automatically dispatched to multiple specialists in parallel." in seen
    assert "Tool note: Specialist rejected: query is empty" in seen
    assert "SYSTEM" not in seen
