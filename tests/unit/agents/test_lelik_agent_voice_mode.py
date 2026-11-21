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


# The keyed call-opening note (decisions/lelik_call_opening.md): LELIK mode only.
_KEY = "able-acorn-absorb"


def _opening_agent(user_config=None, history=()):
    from unittest.mock import AsyncMock, MagicMock

    from src.agents.lelik_agent import LelikAgent
    from src.domain.language import LanguageCode
    from src.domain.lelik_context import LelikContext

    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=list(history))
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(), from_number="+1",
        status_callback_url="https://x/voice/status", prompt_builder=prompt_builder, persona=persona,
        notifications=AsyncMock(), voice_profile=VOICE_PROVIDER_PROFILES["xai"], user_config=user_config,
        system_default_language=LanguageCode.FR, turn_key_source=lambda: _KEY,
    )
    agent.coordinator = None
    return agent, prompt_builder


@pytest.mark.asyncio
async def test_lelik_mode_carries_the_wrapped_opening_with_the_key_that_is_in_the_instructions():
    from src.domain.system_fingerprint import wrap_system
    from src.domain.voice_opening import VOICE_OPENING_FIELD, voice_opening_text
    from src.domain.language import LanguageCode

    agent, prompt_builder = _opening_agent()

    config = await agent.session_config(user_id="u1", account_id="a1", mode=VoiceMode.LELIK)

    assert prompt_builder.build_for_agent.await_args.kwargs["session_key"] == _KEY
    assert config[VOICE_OPENING_FIELD] == wrap_system(voice_opening_text(True, LanguageCode.EN, False), _KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [VoiceMode.INTERPRETER, VoiceMode.TUTOR])
async def test_the_other_modes_carry_no_opening_and_no_key(mode):
    from src.domain.voice_opening import VOICE_OPENING_FIELD

    agent, prompt_builder = _opening_agent()

    config = await agent.session_config(user_id="u1", account_id="a1", mode=mode)

    assert VOICE_OPENING_FIELD not in config
    assert prompt_builder.build_for_agent.await_args.kwargs["session_key"] is None


@pytest.mark.asyncio
async def test_a_fixed_language_user_gets_the_text_in_their_language_else_the_system_default():
    from src.domain.language import LanguageCode
    from src.domain.system_fingerprint import wrap_system
    from src.domain.user import UserBotConfig
    from src.domain.voice_opening import VOICE_OPENING_FIELD, voice_opening_text

    chosen, _ = _opening_agent(UserBotConfig(agent_mirror=False, preferred_language=LanguageCode.ES))
    defaulted, _ = _opening_agent(UserBotConfig(agent_mirror=False))

    assert (await chosen.session_config("u1", "a1"))[VOICE_OPENING_FIELD] == wrap_system(
        voice_opening_text(False, LanguageCode.ES, False), _KEY)
    assert (await defaulted.session_config("u1", "a1"))[VOICE_OPENING_FIELD] == wrap_system(
        voice_opening_text(False, LanguageCode.FR, False), _KEY)


@pytest.mark.asyncio
async def test_a_mirror_user_with_a_message_in_the_history_gets_the_language_sentence():
    from src.domain.voice_opening import MIRROR_TAIL, VOICE_OPENING_FIELD

    agent, _ = _opening_agent(history=[{"role": "alek", "content": "hi"}, {"role": "user", "content": "yo"}])
    without, _ = _opening_agent(history=[{"role": "alek", "content": "hi"}])

    assert MIRROR_TAIL in (await agent.session_config("u1", "a1"))[VOICE_OPENING_FIELD]
    assert MIRROR_TAIL not in (await without.session_config("u1", "a1"))[VOICE_OPENING_FIELD]
