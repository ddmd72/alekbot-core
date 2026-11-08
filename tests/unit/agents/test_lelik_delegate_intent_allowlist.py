"""/voice/delegate enforces the intent allowlist session_config declares: an intent the model names outside it
is refused before any dispatch (the declaration only shapes what the model is told).

The allowlist is the same for every voice mode (lelik / interpreter / tutor): all three run through
LelikAgent.session_config with LELIK.allowed_intents; the mode only picks the prompt profile."""
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import LELIK


def _agent(provider="openai", native_web_search=False) -> LelikAgent:
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=AsyncMock(), persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES[provider], native_web_search=native_web_search,
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result="ok"),
    )
    agent.coordinator = coordinator
    return agent


async def _call(agent, intent):
    return await agent.delegate_outcome(
        user_id="usr-1", account_id="acc-1", arguments={"intent": intent, "query": "q"}, call_context=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", sorted(LELIK.allowed_intents) + ["send_to_chat"])
async def test_a_declared_intent_dispatches_as_before(intent):
    agent = _agent()
    outcome = await _call(agent, intent)
    assert outcome.failed is False and outcome.text == "ok"
    agent.coordinator.handle_delegation.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["send_email", "work_with_drive_files", "search_web", "edit_video"])
async def test_an_intent_outside_the_allowlist_is_refused_and_nothing_dispatches(intent, caplog):
    agent = _agent()
    with caplog.at_level(logging.WARNING):
        outcome = await _call(agent, intent)
    assert outcome.failed is True
    assert outcome.text.startswith("Tool error:")
    agent.coordinator.handle_delegation.assert_not_awaited()
    assert any(intent in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["", "no such intent!! ignore previous", None, 42, ["search_memory"]])
async def test_a_garbage_intent_is_refused(intent):
    agent = _agent()
    outcome = await _call(agent, intent)
    assert outcome.failed is True and outcome.text.startswith("Tool error:")
    agent.coordinator.handle_delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_intent_is_refused():
    agent = _agent()
    outcome = await agent.delegate_outcome("usr-1", "acc-1", {"query": "q"}, call_context=[])
    assert outcome.failed is True and outcome.text.startswith("Tool error:")
    agent.coordinator.handle_delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_web_light_is_refused_when_the_provider_searches_natively():
    # session_config drops it from the declaration in this case; enforcement follows the same rule.
    agent = _agent(provider="xai", native_web_search=True)
    assert VOICE_PROVIDER_PROFILES["xai"].builtin_web_search
    outcome = await _call(agent, "search_web_light")
    assert outcome.failed is True and outcome.text.startswith("Tool error:")
    agent.coordinator.handle_delegation.assert_not_awaited()
    assert (await _call(agent, "search_memory")).failed is False
