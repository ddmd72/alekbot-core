"""Tutor's and DomainResearcher's delegate tools offer only the intents available on the user's profile
(CONFIG_OVERRIDE_HIERARCHY_RFC §3); the gate in `handle_delegation` stays the enforcement. The agent is per user
(`user_id` of its constructor); a message with no such user falls back to the one in its context. Alex Doe is `usr-1`,
Clara is `usr-2`."""
from typing import Optional

import pytest

from src.agents.domain_researcher_agent import DomainResearcherAgent
from src.agents.tutor_agent import TutorAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.infrastructure.agent_manifest import DOMAIN_RESEARCHER, TUTOR, Intent
from tests.support.denied_profile import coordinator_denying, delegate_intents, delegate_tool
from tests.support.scripted_llm import ScriptedLLM, execution_context, text
from tests.support.tool_e2e import StubPromptBuilder

def _tutor(llm, user_id: Optional[str]) -> TutorAgent:
    agent = TutorAgent(AgentConfig(agent_id="tutor_agent", agent_type="tutor"), execution_context(llm, "tutor"),
                       prompt_builder=StubPromptBuilder(), assembler=None, user_id=user_id)
    return agent


def _researcher(llm, user_id: Optional[str]) -> DomainResearcherAgent:
    return DomainResearcherAgent(AgentConfig(agent_id="domain_researcher_agent", agent_type="domain_researcher"),
                                 execution_context(llm, "domain_researcher"), prompt_builder=StubPromptBuilder(),
                                 user_id=user_id)


def _message(intent: str, context_user: str = "usr-1") -> AgentMessage:
    return AgentMessage(
        task_id="t1", sender="coordinator", recipient="agent", intent=AgentIntent.QUERY,
        payload={"query": "help me", "intent": intent},
        context={"account_id": "acc-1", "user_id": context_user, "origin_channel_id": "C1",
                 "origin_platform": "slack", "history": []},
    )


SITES = [
    pytest.param(_tutor, TUTOR, "search_web", id="tutor"),
    pytest.param(_researcher, DOMAIN_RESEARCHER, "open_file", id="domain_researcher"),
]


async def _requests(build, coordinator, user_id: Optional[str], context_user: str = "usr-1"):
    llm = ScriptedLLM([text("ok")])
    agent = build(llm, user_id)
    agent.coordinator = coordinator
    await agent.execute(_message("x", context_user))
    return llm.requests[0]


@pytest.mark.parametrize("build,descriptor,own", SITES)
async def test_an_intent_denied_to_the_user_that_the_site_does_not_offer_changes_nothing(build, descriptor, own):
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    full = [i["name"] for i in coordinator.get_available_intents_for(descriptor)]
    assert full == [own]

    request = await _requests(build, coordinator, "usr-1")

    assert delegate_intents(delegate_tool(request.tools)) == [own]


@pytest.mark.parametrize("build,descriptor,own", SITES)
async def test_a_user_with_no_denies_gets_the_declaration_built_from_the_registry(build, descriptor, own):
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    expected = build(ScriptedLLM([]), "usr-2")._build_delegate_tool_declaration(
        coordinator.get_available_intents_for(descriptor))

    request = await _requests(build, coordinator, "usr-2", "usr-2")

    assert request.tools == [expected]


@pytest.mark.parametrize("build,descriptor,own", SITES)
async def test_an_agent_with_no_user_reads_the_one_of_the_message(build, descriptor, own):
    request = await _requests(build, coordinator_denying({"usr-1": {own}}), None, "usr-1")

    assert not request.tools


@pytest.mark.parametrize("build,descriptor,own", SITES)
async def test_with_the_only_offered_intent_denied_no_delegate_tool_is_declared(build, descriptor, own):
    request = await _requests(build, coordinator_denying({"usr-1": {own}}), "usr-1")

    assert not request.tools
