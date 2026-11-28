"""Notes' own delegation (the `delegate_to_specialist` tool of its model): the declaration lists only the intents the
user's profile leaves on, a refused or failed delegation reaches its model as `Tool error: <error>`, and a success with
no result stays "No result" (CONFIG_OVERRIDE_HIERARCHY_RFC §3). Real Notes and coordinator, a scripted model, a recording
stand-in for the delegated specialist."""
from __future__ import annotations

from src.domain.agent import AgentMessage, AgentResponse
from src.infrastructure.agent_manifest import COMPUTE
from tests.support.denied_profile import denying
from tests.support.scripted_llm import text, tool_results, tools
from tests.support.seam_world import StandInAgent
from tests.unit.agents.test_notes_agent_tool_fields_e2e import USER, Rig


def _compute_answers(result):
    def respond(message: AgentMessage) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=message.recipient, result=result)
    return respond


async def _rig(*script, deny=()):
    rig = await Rig(list(script)).start()
    rig.coordinator._registry.register(COMPUTE)
    rig.agent.coordinator = rig.coordinator
    rig.compute = StandInAgent(f"compute_agent_{USER}", "compute", rig.coordinator)
    rig.coordinator.register_agent(rig.compute)  # type: ignore[arg-type]
    if deny:
        rig.coordinator._effective_config = denying(*deny)
    return rig


def _delegation(intent: str):
    return tools(("delegate_to_specialist", {"intent": intent, "query": "q"}))


def _declared(rig) -> str:
    return str(rig.llm.requests[0].tools)


async def test_a_denied_intent_is_refused_to_the_notes_model_and_never_runs():
    rig = await _rig(_delegation("compute"), text("done"), deny=("compute",))

    await rig.delegate()

    assert rig.results_of("delegate_to_specialist") == ["Tool error: compute is not available on this profile."]
    assert rig.compute.received == []
    assert rig.finished()


async def test_a_denied_intent_is_absent_from_the_declaration_and_the_others_stay():
    rig = await _rig(text("done"), deny=("compute",))

    await rig.delegate()

    assert "- compute_datetime:" in _declared(rig) and "- compute:" not in _declared(rig)


async def test_without_denies_the_declaration_is_unchanged_and_the_delegation_runs():
    rig = await _rig(_delegation("compute"), text("done"))
    rig.compute.always(_compute_answers("42"))

    await rig.delegate()

    assert "- compute_datetime:" in _declared(rig) and "- compute:" in _declared(rig)
    assert rig.results_of("delegate_to_specialist") == [42]
    assert len(rig.compute.received) == 1


async def test_with_every_intent_denied_the_delegate_tool_is_omitted():
    rig = await _rig(text("done"), deny=("compute", "compute_datetime"))

    await rig.delegate()

    assert "delegate_to_specialist" not in _declared(rig)


async def test_a_generic_failure_reaches_the_model_as_a_tool_error():
    rig = await _rig(_delegation("no_such_intent"), text("done"))

    await rig.delegate()

    assert rig.results_of("delegate_to_specialist") == ["Tool error: No agent registered for intent: no_such_intent"]


async def test_a_success_without_a_result_stays_no_result():
    rig = await _rig(_delegation("compute"), text("done"))
    rig.compute.always(_compute_answers(""))

    await rig.delegate()

    assert rig.results_of("delegate_to_specialist") == ["No result"]
