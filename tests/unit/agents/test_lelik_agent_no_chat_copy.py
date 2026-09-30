"""Lelik's own lookups post nothing to chat (VOICE_COMPANION_RFC §4.15.3, owner 2026-09-29).

The automatic link copy of §4.10 rule 2 filled the chat with every search's links. When the
caller wants something in chat, Lelik hands it to Alek (tell_alek).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.agent import AgentResponse
from src.infrastructure.agent_coordinator import AgentCoordinator

_FINDINGS = ('{"findings": [{"text": "Sunny", "source": "AEMET", "url": "https://www.aemet.es/x"}], '
             '"conclusion": "See https://example.com/forecast"}')


def _agent():
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    notifications = AsyncMock()
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=AsyncMock(), persona=persona, notifications=notifications,
        voice_profile=VOICE_PROVIDER_PROFILES["openai"],
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result=_FINDINGS))
    agent.coordinator = coordinator
    return agent, notifications


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["search_web_light", "search_memory"])
async def test_a_result_full_of_links_posts_nothing(intent):
    agent, notifications = _agent()

    outcome = await agent.delegate_outcome(user_id="u1", account_id="a1",
                                           arguments={"intent": intent, "query": "weather"}, call_context=[])

    assert "aemet.es" in outcome.text  # Lelik still gets the result to speak from
    notifications.notify_answer_copy.assert_not_awaited()
    notifications.notify_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_light_search_does_not_fan_out_to_maps():
    agent, _ = _agent()

    await agent.delegate_outcome(user_id="u1", account_id="a1",
                                 arguments={"intent": "search_web_light", "query": "weather"}, call_context=[])

    assert {c.kwargs["intent"] for c in agent.coordinator.handle_delegation.await_args_list} == {"search_web_light"}
