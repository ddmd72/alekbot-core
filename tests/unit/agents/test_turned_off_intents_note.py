"""The delegate tool says when the user's profile turned intents off (agents_registry in the system prompt still describes
them), and a live voice call refuses a turned-off intent in words that name none. Alex Doe is `usr-1`, Clara `usr-2`."""
from unittest.mock import AsyncMock

from src.agents.base_agent import BaseAgent
from src.domain.intent_access_texts import no_intents_line, turned_off_intents_note, voice_intent_unavailable_text
from src.domain.agent import AgentResponse
from src.infrastructure.agent_manifest import QUICK_RESPONSE, SMART_RESPONSE, Intent
from tests.support.denied_profile import coordinator_denying, delegate_tool
from tests.support.scripted_llm import ScriptedLLM, text
from tests.support.tool_e2e import smart_message, smart_rig
from tests.unit.agents.core.test_quick_offers_available_intents import _message as quick_message, _quick
from tests.unit.agents.test_lelik_offers_available_intents import _lelik

NOTE = (
    "agents_registry also describes intents missing from this list: they are turned off for this user; "
    "do not call them, and if the user asks for one, say it is turned off."
)
NONE_LINE = "(none: every specialist is turned off for this user)"
VOICE_REFUSAL = "Tool error: this request is turned off for this caller; asking again gets the same answer."
SAMPLE = [{"name": "search_memory", "description": "memory"}]


def test_the_texts_are_the_agreed_wording():
    assert turned_off_intents_note() == NOTE
    assert no_intents_line() == NONE_LINE
    assert voice_intent_unavailable_text() == VOICE_REFUSAL


def test_the_builder_without_turned_off_is_unchanged():
    tool = BaseAgent._build_delegate_tool_declaration(SAMPLE)

    assert tool["description"].endswith("per-intent query formulation rules and required context fields.")
    assert NOTE not in tool["description"]
    assert "(no specialist agents registered)" in BaseAgent._build_delegate_tool_declaration([])["description"]


def test_the_builder_with_turned_off_appends_the_sentence_after_the_registry_pointer():
    description = BaseAgent._build_delegate_tool_declaration(SAMPLE, turned_off=True)["description"]

    assert description.endswith(f"required context fields. {NOTE}")
    assert "- search_memory: memory" in description


def test_the_builder_with_every_intent_turned_off_says_none():
    description = BaseAgent._build_delegate_tool_declaration([], turned_off=True)["description"]

    assert f"Available intents:\n{NONE_LINE}\n\n" in description
    assert description.endswith(NOTE) and "(no specialist agents registered)" not in description


# --- Smart --------------------------------------------------------------------------------------------------------

async def _smart_tool(coordinator, user_id="usr-1", flag=False):
    from src.domain.user import UserBotConfig
    llm = ScriptedLLM([text("ok")])
    agent = smart_rig(llm, user_id=user_id, coordinator=coordinator, user_config=UserBotConfig(external_tasks_enabled=flag))
    await agent.execute(smart_message("hello", user_id=user_id, account_id="acc-1", session_id=f"{user_id}:C1",
                                      origin_platform="slack", origin_channel_id="C1"))
    return delegate_tool(llm.requests[0].tools)


async def test_smart_says_so_when_the_profile_removed_an_intent():
    tool = await _smart_tool(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}}))

    assert tool["description"].endswith(NOTE)


async def test_smart_says_nothing_to_a_user_with_no_denies():
    tool = await _smart_tool(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}}), "usr-2")

    assert NOTE not in tool["description"]


async def test_smart_does_not_say_it_when_only_the_external_task_offer_is_removed():
    tool = await _smart_tool(coordinator_denying({"usr-1": {Intent.WORK_WITH_EXTERNAL_TASKS}}), flag=True)

    assert Intent.WORK_WITH_EXTERNAL_TASKS not in tool["description"] and NOTE not in tool["description"]


async def test_smart_with_every_intent_turned_off_still_declares_the_tool_with_the_none_line():
    names = [i["name"] for i in coordinator_denying({}).get_available_intents_for(SMART_RESPONSE)]

    tool = await _smart_tool(coordinator_denying({"usr-1": names}))

    assert f"Available intents:\n{NONE_LINE}\n\n" in tool["description"] and tool["description"].endswith(NOTE)


# --- Quick --------------------------------------------------------------------------------------------------------

async def _quick_tool(coordinator, user_id="usr-1"):
    llm = ScriptedLLM([text("ok")])
    await _quick(llm, coordinator, user_id).execute(quick_message(user_id))
    return delegate_tool(llm.requests[0].tools)


async def test_quick_says_so_only_when_the_profile_removed_an_intent():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})

    assert (await _quick_tool(coordinator))["description"].endswith(NOTE)
    assert NOTE not in (await _quick_tool(coordinator, "usr-2"))["description"]


async def test_quick_with_every_intent_turned_off_still_declares_the_tool_with_the_none_line():
    names = [i["name"] for i in coordinator_denying({}).get_available_intents_for(QUICK_RESPONSE)]

    tool = await _quick_tool(coordinator_denying({"usr-1": names}))

    assert f"Available intents:\n{NONE_LINE}\n\n" in tool["description"] and tool["description"].endswith(NOTE)


# --- Lelik --------------------------------------------------------------------------------------------------------

async def _lelik_tool(agent, user_id="usr-1"):
    config = await agent.session_config(user_id=user_id, account_id="acc-1")
    return delegate_tool(config["tools"])


async def test_lelik_says_so_when_the_profile_removed_an_intent():
    tool = await _lelik_tool(_lelik(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})))

    assert tool["description"].endswith(NOTE)


async def test_lelik_does_not_say_it_when_only_the_native_search_removal_applies():
    tool = await _lelik_tool(_lelik(coordinator_denying({}), "xai", native_web_search=True))

    assert Intent.SEARCH_WEB_LIGHT not in tool["description"] and NOTE not in tool["description"]


async def test_lelik_says_it_once_when_both_causes_apply():
    tool = await _lelik_tool(_lelik(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}}), "xai", native_web_search=True))

    assert tool["description"].count(NOTE) == 1 and Intent.SEARCH_WEB_LIGHT not in tool["description"]


async def _delegate(agent, intent, user_id="usr-1"):
    agent._persona.primary_channel = AsyncMock(return_value=None)
    agent.coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result="found"))
    outcome = await agent.delegate_outcome(
        user_id=user_id, account_id="acc-1", arguments={"intent": intent, "query": "q"}, call_context=[])
    return outcome, agent.coordinator.handle_delegation


async def test_lelik_refuses_a_turned_off_intent_before_dispatching_and_names_none():
    agent = _lelik(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}}))

    outcome, dispatched = await _delegate(agent, Intent.SEARCH_MEMORY)

    assert outcome.text == VOICE_REFUSAL and outcome.failed is True
    dispatched.assert_not_awaited()


async def test_lelik_dispatches_an_available_intent_as_before():
    agent = _lelik(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}}))

    outcome, dispatched = await _delegate(agent, Intent.SEARCH_MEMORY, "usr-2")

    assert outcome.failed is False and outcome.text == "found"
    dispatched.assert_awaited_once()
