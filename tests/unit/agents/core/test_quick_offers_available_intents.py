"""Quick's delegate tool offers only the intents available on the user's profile (CONFIG_OVERRIDE_HIERARCHY_RFC §3);
the gate in `handle_delegation` stays the enforcement. Alex Doe is `usr-1`, Clara is `usr-2`."""
from src.agents.core.quick_response_agent import QuickResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.user import PerformanceTier
from src.infrastructure.agent_manifest import QUICK_RESPONSE, Intent
from src.ports.llm_port import ProviderCapabilities
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.ports.llm_port import AgentExecutionContext
from tests.support.intent_access_world import coordinator_denying, delegate_intents, delegate_tool
from tests.support.scripted_llm import ScriptedLLM, text
from tests.support.tool_e2e import EmptySessions, StubPromptBuilder


def _quick(llm, coordinator, user_id="usr-1") -> QuickResponseAgent:
    ctx = AgentExecutionContext(
        agent_type="quick", provider=llm, model_name="scripted-model", tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )
    agent = QuickResponseAgent(
        config=AgentConfig(agent_id="quick_response_agent", agent_type="quick_response", llm_model="scripted-model",
                           timeout_ms=10000, capabilities=["fast_response"], metadata={"user_id": user_id}),
        execution_context=ctx, session_store=EmptySessions(), prompt_builder=StubPromptBuilder(),
    )
    agent.coordinator = coordinator
    return agent


def _message(user_id="usr-1") -> AgentMessage:
    return AgentMessage(
        task_id="t1", sender="user", recipient="quick_response_agent", intent=AgentIntent.QUERY,
        payload={"text": "hello"}, context={"session_id": f"{user_id}:C1", "user_id": user_id, "account_id": "acc-1"},
    )


async def _offered(coordinator, user_id="usr-1") -> dict:
    llm = ScriptedLLM([text("ok")])
    await _quick(llm, coordinator, user_id).execute(_message(user_id))
    return delegate_tool(llm.requests[0].tools)


async def test_an_intent_denied_to_the_user_is_not_offered_and_the_rest_is():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    full = [i["name"] for i in coordinator.get_available_intents_for(QUICK_RESPONSE)]
    assert Intent.SEARCH_MEMORY in full

    tool = await _offered(coordinator)

    assert delegate_intents(tool) == [name for name in full if name != Intent.SEARCH_MEMORY]


async def test_a_user_with_no_denies_gets_the_declaration_built_from_the_registry():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    expected = QuickResponseAgent._build_delegate_tool_declaration(
        coordinator.get_available_intents_for(QUICK_RESPONSE))

    assert await _offered(coordinator, "usr-2") == expected


async def test_with_every_intent_denied_no_delegate_tool_is_declared():
    names = [i["name"] for i in coordinator_denying({}).get_available_intents_for(QUICK_RESPONSE)]
    llm = ScriptedLLM([text("ok")])

    await _quick(llm, coordinator_denying({"usr-1": names})).execute(_message())

    assert not [t for t in (llm.requests[0].tools or []) if t["name"] == "delegate_to_specialist"]
