"""relay_main's per-call adapter registry (VOICE_MULTI_PROVIDER_RFC §4.2/§4.3)."""
import pytest

import relay_main
from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.voice_audio_format import PCM16_24K
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_session_spec import VoiceSessionSpec
from src.domain.voice_turn_ownership import TurnOwnership

_KEYS = {"openai": "sk-o", "xai": "xai-k"}


class TestRelaySessionFactory:
    def test_every_profile_has_an_adapter_that_implements_its_turn_ownership(self):
        for name, profile in VOICE_PROVIDER_PROFILES.items():
            adapter_cls = relay_main._ADAPTERS[profile.session.provider]
            assert profile.session.turn_ownership in adapter_cls.supported_turn_ownership, name

    @pytest.mark.parametrize("provider,cls", [("openai", OpenAIRealtimeAdapter), ("xai", XaiRealtimeAdapter)])
    def test_builds_the_calls_provider_with_its_key_voice_and_format(self, provider, cls):
        spec = VOICE_PROVIDER_PROFILES[provider].session

        adapter = relay_main._realtime_session_factory(_KEYS, PCM16_24K)(spec)

        assert isinstance(adapter, cls)
        assert adapter._api_key == _KEYS[provider]
        assert adapter._voice == spec.voice
        assert adapter._audio_format == PCM16_24K

    def test_refuses_a_turn_ownership_the_adapter_does_not_implement(self):
        spec = VoiceSessionSpec("openai", TurnOwnership.PROVIDER, "verse", "medium")
        with pytest.raises(ValueError):
            relay_main._realtime_session_factory(_KEYS, PCM16_24K)(spec)
