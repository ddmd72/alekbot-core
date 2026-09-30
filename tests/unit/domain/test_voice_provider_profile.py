"""VoiceProviderProfile registry and resolution (VOICE_MULTI_PROVIDER_RFC §4.1)."""
import pytest

from src.domain.voice_provider_profile import (
    DEFAULT_VOICE_PROVIDER,
    LEGACY_VOICE_SESSION,
    VOICE_PROVIDER_PROFILES,
    resolve_voice_profile,
)
from src.domain.voice_session_spec import VoiceSessionSpec
from src.domain.voice_turn_ownership import TurnOwnership


class TestVoiceProviderProfile:
    def test_default_is_the_xai_call_four_setup(self):
        profile = VOICE_PROVIDER_PROFILES[DEFAULT_VOICE_PROVIDER]
        assert DEFAULT_VOICE_PROVIDER == "xai"
        assert profile.prompt_profile == "lelik_you"
        assert profile.session == VoiceSessionSpec("xai", TurnOwnership.PROVIDER, "castor", "high")

    def test_openai_keeps_the_relay_owned_lelik(self):
        profile = VOICE_PROVIDER_PROFILES["openai"]
        assert profile.prompt_profile == "lelik"
        assert profile.session == VoiceSessionSpec("openai", TurnOwnership.RELAY, "verse", "medium")

    @pytest.mark.parametrize("value", [None, "", "gemini-someday"])
    def test_unset_or_unknown_resolves_to_the_default(self, value):
        assert resolve_voice_profile(value) is VOICE_PROVIDER_PROFILES[DEFAULT_VOICE_PROVIDER]

    @pytest.mark.parametrize("provider", sorted(VOICE_PROVIDER_PROFILES))
    def test_a_known_provider_resolves_to_itself(self, provider):
        assert resolve_voice_profile(provider) is VOICE_PROVIDER_PROFILES[provider]

    @pytest.mark.parametrize("provider", sorted(VOICE_PROVIDER_PROFILES))
    def test_the_profile_key_names_its_own_provider(self, provider):
        assert VOICE_PROVIDER_PROFILES[provider].session.provider == provider

    def test_legacy_session_is_openai_relay_owned(self):
        assert LEGACY_VOICE_SESSION == VOICE_PROVIDER_PROFILES["openai"].session


class TestVoiceSessionSpec:
    def test_round_trips_through_the_session_config_dict(self):
        spec = VoiceSessionSpec("xai", TurnOwnership.PROVIDER, "castor", "high")
        wire = spec.to_dict()
        assert wire == {"provider": "xai", "turn_ownership": "provider", "voice": "castor", "reasoning_effort": "high"}
        assert VoiceSessionSpec.from_dict(wire) == spec
