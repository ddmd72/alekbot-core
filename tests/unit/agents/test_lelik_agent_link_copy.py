"""What is left of the chat copy on Lelik's side: none (VOICE_COMPANION_RFC §4.15.3 withdrew the
automatic link copy). ask_alek's own copy is AlekGatewayAgent's, never doubled here."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.infrastructure.agent_coordinator import AgentCoordinator


def _agent(delegation_result: str):
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
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result=delegation_result),
    )
    agent.coordinator = coordinator
    return agent, notifications, coordinator


@pytest.mark.asyncio
async def test_result_without_urls_posts_no_copy():
    agent, notifications, _ = _agent("sunny, 24°C in Valencia")

    await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "weather"},
        call_context=[],
    )

    notifications.notify_answer_copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_alek_never_double_posts_even_with_urls_present():
    """AlekGatewayAgent already runs notify_answer_copy for ask_alek's own structured answer."""
    result_str = '{"findings": [{"source": "x", "url": "https://example.com/x"}]}'
    agent, notifications, _ = _agent(result_str)

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "ask_alek", "query": "find me links"},
        call_context=[],
    )

    assert output == result_str
    notifications.notify_answer_copy.assert_not_awaited()
