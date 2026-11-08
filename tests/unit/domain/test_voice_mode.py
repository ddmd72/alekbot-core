"""Voice modes: which prompt profile each provider serves per mode, and which modes end with a summary."""
import pytest

from src.domain.voice_mode import VOICE_MODES_WITH_CALL_SUMMARY, UnsupportedVoiceMode, VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES


def test_only_the_companion_call_gets_a_summary():
    assert VOICE_MODES_WITH_CALL_SUMMARY == frozenset({VoiceMode.LELIK})


@pytest.mark.parametrize("provider", sorted(VOICE_PROVIDER_PROFILES))
def test_lelik_mode_is_the_providers_own_profile(provider):
    profile = VOICE_PROVIDER_PROFILES[provider]
    assert profile.prompt_profile_for(VoiceMode.LELIK) == profile.prompt_profile


def test_xai_serves_a_profile_per_mode():
    profile = VOICE_PROVIDER_PROFILES["xai"]
    assert profile.prompt_profile_for(VoiceMode.INTERPRETER) == "lelik_xai_interpreter"
    assert profile.prompt_profile_for(VoiceMode.TUTOR) == "lelik_xai_tutor"


@pytest.mark.parametrize("mode", [VoiceMode.INTERPRETER, VoiceMode.TUTOR])
def test_openai_has_no_mode_profiles(mode):
    with pytest.raises(UnsupportedVoiceMode):
        VOICE_PROVIDER_PROFILES["openai"].prompt_profile_for(mode)


def test_unsupported_mode_is_a_value_error():
    assert issubclass(UnsupportedVoiceMode, ValueError)
