"""prompt_profile: Lelik's session prompt can be built from another profile (the lelik_bare experiment)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.lelik_context import LelikContext


def _agent(**kwargs):
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(), **kwargs,
    )
    agent.coordinator = None
    return agent, prompt_builder


class TestLelikPromptProfile:
    @pytest.mark.asyncio
    async def test_default_profile_is_lelik(self):
        agent, prompt_builder = _agent()

        await agent.session_config(user_id="u1", account_id="a1")

        assert prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == "lelik"

    @pytest.mark.asyncio
    async def test_another_profile_is_used_when_given(self):
        agent, prompt_builder = _agent(prompt_profile="lelik_bare")

        await agent.session_config(user_id="u1", account_id="a1")

        assert prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == "lelik_bare"
