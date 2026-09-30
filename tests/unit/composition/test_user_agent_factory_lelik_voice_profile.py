"""_build_lelik injects the user's VoiceProviderProfile (VOICE_MULTI_PROVIDER_RFC §4.2)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.user import UserBotConfig
from src.domain.voice_provider_profile import DEFAULT_VOICE_PROVIDER, VOICE_PROVIDER_PROFILES


def _build(voice_provider=None):
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid", voice_provider=voice_provider)
    ctx = _UserContext(user_profile=profile, prompt_builder=MagicMock())
    fake = SimpleNamespace(
        _telephony=MagicMock(),
        config={"TWILIO_PHONE_NUMBER": "+346001", "CLOUD_RUN_SERVICE_URL": "https://main.example.com"},
        notification_service=MagicMock(),
        repository=MagicMock(),
        session_store=MagicMock(),
    )
    return UserAgentFactory._build_lelik(fake, "u1", ctx)


class TestLelikVoiceProfileWiring:
    def test_no_choice_gets_the_default_profile(self):
        assert _build()._voice_profile is VOICE_PROVIDER_PROFILES[DEFAULT_VOICE_PROVIDER]

    @pytest.mark.parametrize("provider", sorted(VOICE_PROVIDER_PROFILES))
    def test_the_users_choice_is_injected(self, provider):
        assert _build(provider)._voice_profile is VOICE_PROVIDER_PROFILES[provider]

    def test_an_unknown_choice_falls_back_to_the_default_and_logs(self, caplog):
        agent = _build("no-such-provider")
        assert agent._voice_profile is VOICE_PROVIDER_PROFILES[DEFAULT_VOICE_PROVIDER]
        assert "no-such-provider" in caplog.text
