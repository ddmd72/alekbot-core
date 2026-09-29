"""A failed delegation result never posts a chat copy, even when its error string contains a
URL (e.g. an OpenAI 429 pointing at platform.openai.com) — VOICE_COMPANION_RFC §4.10 rule 2,
Important finding 1."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.infrastructure.agent_coordinator import AgentCoordinator


def _agent():
    prompt_builder = AsyncMock()
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    notifications = AsyncMock()
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=notifications,
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    agent.coordinator = coordinator
    return agent, notifications, coordinator


@pytest.mark.asyncio
async def test_rejected_result_with_a_url_in_the_error_posts_no_copy():
    agent, notifications, coordinator = _agent()
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.failure(
        task_id="t", agent_id="a",
        error="rate limited, see https://platform.openai.com/account/limits",
    ))

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "q"}, call_context=[],
    )

    assert "platform.openai.com" in output  # the spoken/returned result is untouched
    notifications.notify_answer_copy.assert_not_awaited()
