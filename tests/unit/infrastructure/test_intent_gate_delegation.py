"""A delegation to an intent the user's effective configuration turns off never runs, and the model reads a refusal
that retrying cannot fix (CONFIG_OVERRIDE_HIERARCHY_RFC §3). One test per path into `handle_delegation`: the engine's
single dispatch (SYNC and ASYNC) and the fan-out. Lelik's voice dispatch refuses before it reaches the coordinator
(tests/unit/agents/test_turned_off_intents_note.py). Real coordinator and registry over the seam
world; the specialists are recording stand-ins, so "did not run" is `received == []`."""
from __future__ import annotations

from typing import Any, Dict

from src.domain.agent import AgentMessage, AgentResponse
from src.domain.intent_access_texts import intent_unavailable_text
from src.domain.llm import ToolCall
from src.infrastructure.agent_manifest import SMART_RESPONSE
from src.infrastructure.delegation_engine import DelegationEngine
from tests.support.denied_profile import denying, with_user_entry
from tests.support.seam_world import ACCOUNT, USER, SeamWorld


def _refusal(intent: str) -> str:
    return f"Tool error: {intent_unavailable_text(intent)}"


def _answers(text: str):
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=message.recipient, result=text)
    return respond


def _call(intent: str) -> ToolCall:
    return ToolCall(name="delegate_to_specialist", args={"intent": intent, "query": "q"})


def _context() -> Dict[str, Any]:
    return {"user_id": USER, "account_id": ACCOUNT}


async def _dispatch(world: SeamWorld, intent: str, fanout: bool = False):
    return await DelegationEngine(world.coordinator).dispatch(
        _call(intent), _context(), {}, dict(SMART_RESPONSE.intent_fanout) if fanout else {}, "smart_response_agent",
    )


async def test_a_denied_sync_intent_is_a_tool_error_and_the_specialist_never_runs(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = denying("search_memory")
    memory = world.agent("facts_memory_agent").always(_answers("facts"))

    result = await _dispatch(world, "search_memory")

    assert result.result_str == _refusal("search_memory")
    assert result.failed is True
    assert "Correct your input" not in result.result_str
    assert memory.received == []


async def test_a_denied_async_intent_is_refused_and_nothing_is_enqueued(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = denying("create_document")
    planner = world.agent("doc_planner_agent").always(_answers("plan"))

    result = await _dispatch(world, "create_document")

    assert result.result_str == _refusal("create_document")
    assert result.failed is True
    assert world.requests() == []
    assert planner.received == []


async def test_a_denied_primary_of_a_fanout_refuses_and_dispatches_no_secondary(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = denying("search_web")
    web = world.agent("web_search_agent").always(_answers("web"))
    maps = world.agent("maps_search_agent").always(_answers("maps"))

    result = await _dispatch(world, "search_web", fanout=True)

    assert result.result_str == _refusal("search_web")
    assert result.failed is True
    assert web.received == [] and maps.received == []


async def test_a_denied_secondary_of_a_fanout_leaves_the_primary_result_only(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = denying("maps_query")
    web = world.agent("web_search_agent").always(_answers("web answer"))
    maps = world.agent("maps_search_agent").always(_answers("maps answer"))

    result = await _dispatch(world, "search_web", fanout=True)

    assert result.failed is False
    assert "web answer" in result.result_str and "maps answer" not in result.result_str
    assert len(web.received) == 1 and maps.received == []


async def test_an_intent_the_system_level_does_not_gate_runs_whatever_the_user_entry_says(monkeypatch):
    """`generate_docx_code` is `access_gated=False`, so no SYSTEM key exists for it and a user entry cannot turn it off."""
    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = with_user_entry({"search_memory": True}, {"generate_docx_code": False})
    world.agent("doc_generator_agent").always(_answers("docx"))

    result = await _dispatch(world, "generate_docx_code")

    assert result.failed is False
    assert len(world.requests("agent_execution")) == 1


async def test_an_available_intent_runs_unchanged(monkeypatch):
    world = SeamWorld(monkeypatch)
    world.coordinator._effective_config = denying("tutor_chat")
    memory = world.agent("facts_memory_agent").always(_answers("facts"))

    result = await _dispatch(world, "search_memory")

    assert result.failed is False and "facts" in result.result_str
    assert len(memory.received) == 1
