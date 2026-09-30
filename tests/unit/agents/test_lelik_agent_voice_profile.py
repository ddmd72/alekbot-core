"""LelikAgent.session_config builds from the profile's prompt and carries its session spec."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.lelik_context import LelikContext
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES


def _agent(voice_profile):
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=voice_profile,
    )
    agent.coordinator = None
    return agent, prompt_builder


class TestLelikSessionConfigVoice:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", sorted(VOICE_PROVIDER_PROFILES))
    async def test_prompt_profile_and_session_spec_come_from_the_voice_profile(self, provider):
        profile = VOICE_PROVIDER_PROFILES[provider]
        agent, prompt_builder = _agent(profile)

        config = await agent.session_config(user_id="u1", account_id="a1")

        assert prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == profile.prompt_profile
        assert config["voice"] == profile.session.to_dict()
