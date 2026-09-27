"""delegate_outcome surfaces whether a Lelik delegation failed, so /voice/delegate never keeps an
error string for chat (voice UAT round 1, Task 2 fix 1). delegate() keeps returning the text."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.voice_delegation_outcome import VoiceDelegationOutcome
from src.infrastructure.agent_coordinator import AgentCoordinator


def _agent(response: AgentResponse) -> LelikAgent:
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=AsyncMock(), persona=persona, notifications=AsyncMock(),
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(return_value=response)
    agent.coordinator = coordinator
    return agent


_ARGS = {"intent": "search_memory", "query": "q"}


@pytest.mark.asyncio
async def test_successful_delegation_is_not_failed():
    agent = _agent(AgentResponse.success(task_id="t", agent_id="a", result="sunny"))
    outcome = await agent.delegate_outcome(user_id="u1", account_id="a1", arguments=_ARGS, call_context=[])
    assert outcome == VoiceDelegationOutcome(text="sunny", failed=False)


@pytest.mark.asyncio
async def test_failed_delegation_is_flagged():
    agent = _agent(AgentResponse.failure(task_id="t", agent_id="a", error="specialist timed out"))
    outcome = await agent.delegate_outcome(user_id="u1", account_id="a1", arguments=_ARGS, call_context=[])
    assert outcome.failed is True
    assert outcome.text  # the error text Lelik speaks as "it did not go through"


@pytest.mark.asyncio
async def test_delegate_still_returns_the_text():
    agent = _agent(AgentResponse.failure(task_id="t", agent_id="a", error="specialist timed out"))
    text = await agent.delegate(user_id="u1", account_id="a1", arguments=_ARGS, call_context=[])
    outcome = await agent.delegate_outcome(user_id="u1", account_id="a1", arguments=_ARGS, call_context=[])
    assert isinstance(text, str) and text == outcome.text
