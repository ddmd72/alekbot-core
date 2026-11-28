"""Smart's delegate tool offers only the intents available on the user's profile, the external task specialist's intent
included (CONFIG_OVERRIDE_HIERARCHY_RFC §3); the gate in `handle_delegation` stays the enforcement. Alex Doe is
`usr-1`, Clara is `usr-2`."""
from src.agents.core.smart_response_agent import _EXTERNAL_TASKS_OFFER
from src.domain.agent import AgentStatus
from src.domain.intent_access_texts import no_intents_line
from src.domain.user import UserBotConfig
from src.infrastructure.agent_manifest import SMART_RESPONSE, Intent
from tests.support.denied_profile import coordinator_denying, delegate_intents, delegate_tool
from tests.support.scripted_llm import ScriptedLLM, text
from tests.support.tool_e2e import smart_message, smart_rig

QUEUE = Intent.WORK_WITH_EXTERNAL_TASKS


async def _offered(coordinator, user_id="usr-1", flag=False) -> dict:
    llm = ScriptedLLM([text("ok")])
    agent = smart_rig(llm, user_id=user_id, coordinator=coordinator,
                      user_config=UserBotConfig(external_tasks_enabled=flag))
    await agent.execute(smart_message("hello", user_id=user_id, account_id="acc-1", session_id=f"{user_id}:C1",
                                      origin_platform="slack", origin_channel_id="C1"))
    return delegate_tool(llm.requests[0].tools)


async def test_an_intent_denied_to_the_user_is_not_offered_and_the_rest_is():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    full = [i["name"] for i in coordinator.get_available_intents_for(SMART_RESPONSE)]
    assert Intent.SEARCH_MEMORY in full

    tool = await _offered(coordinator)

    assert delegate_intents(tool) == [name for name in full if name != Intent.SEARCH_MEMORY]


async def test_a_user_with_no_denies_gets_the_declaration_built_from_the_registry():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    agent = smart_rig(ScriptedLLM([]), coordinator=coordinator)
    expected = agent._get_tool_declarations(False)

    assert [await _offered(coordinator, "usr-2")] == expected


async def test_the_task_queue_intent_is_offered_to_a_flagged_user_who_has_it():
    tool = await _offered(coordinator_denying({}), flag=True)

    assert QUEUE in delegate_intents(tool)


async def test_the_task_queue_intent_denied_to_the_user_is_not_offered_though_the_flag_is_on():
    coordinator = coordinator_denying({"usr-1": {QUEUE}})
    own = [i["name"] for i in coordinator.get_available_intents_for(SMART_RESPONSE)]

    tool = await _offered(coordinator, flag=True)

    assert delegate_intents(tool) == own


async def test_the_task_queue_intent_is_still_offered_to_another_flagged_user():
    coordinator = coordinator_denying({"usr-1": {QUEUE}})

    assert QUEUE in delegate_intents(await _offered(coordinator, "usr-2", flag=True))


async def test_with_every_intent_denied_the_delegate_tool_is_declared_with_no_intent():
    coordinator = coordinator_denying({})
    names = [i["name"] for i in coordinator.get_available_intents_for(SMART_RESPONSE)]
    llm = ScriptedLLM([text("ok")])
    agent = smart_rig(llm, user_id="usr-1", coordinator=coordinator_denying({"usr-1": names}))

    response = await agent.execute(smart_message("hello", user_id="usr-1", account_id="acc-1", session_id="usr-1:C1",
                                                 origin_platform="slack", origin_channel_id="C1"))

    assert response.status == AgentStatus.SUCCESS
    tool = delegate_tool(llm.requests[0].tools)
    assert delegate_intents(tool) == []
    assert f"Available intents:\n{no_intents_line()}\n\n" in tool["description"]


def test_without_a_user_the_eval_scripts_call_offers_the_whole_registry():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    agent = smart_rig(ScriptedLLM([]), coordinator=coordinator)

    (tool,) = agent._get_tool_declarations(True)

    assert delegate_intents(tool) == [
        i["name"] for i in [*coordinator.get_available_intents_for(SMART_RESPONSE),
                            *coordinator.get_available_intents_for(_EXTERNAL_TASKS_OFFER)]]


async def test_a_registry_with_no_intent_keeps_the_empty_delegate_tool_as_before():
    from src.infrastructure.agent_coordinator import AgentCoordinator

    assert delegate_intents(await _offered(AgentCoordinator())) == []
