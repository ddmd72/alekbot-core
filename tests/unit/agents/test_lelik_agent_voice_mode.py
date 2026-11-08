"""LelikAgent.session_config builds the instructions from the mode's prompt profile."""
import pytest

from src.domain.voice_mode import UnsupportedVoiceMode, VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from tests.unit.agents.test_lelik_agent_voice_profile import _agent


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [
    (VoiceMode.LELIK, "lelik_xai"),
    (VoiceMode.INTERPRETER, "lelik_xai_interpreter"),
    (VoiceMode.TUTOR, "lelik_xai_tutor"),
])
async def test_session_config_uses_the_modes_profile(mode, expected):
    agent, prompt_builder = _agent(VOICE_PROVIDER_PROFILES["xai"])

    await agent.session_config(user_id="u1", account_id="a1", mode=mode)

    assert prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == expected


@pytest.mark.asyncio
async def test_default_mode_is_lelik():
    agent, prompt_builder = _agent(VOICE_PROVIDER_PROFILES["xai"])

    await agent.session_config(user_id="u1", account_id="a1")

    assert prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == "lelik_xai"


@pytest.mark.asyncio
async def test_a_provider_without_the_mode_raises_before_building():
    agent, prompt_builder = _agent(VOICE_PROVIDER_PROFILES["openai"])

    with pytest.raises(UnsupportedVoiceMode):
        await agent.session_config(user_id="u1", account_id="a1", mode=VoiceMode.TUTOR)
    prompt_builder.build_for_agent.assert_not_awaited()
