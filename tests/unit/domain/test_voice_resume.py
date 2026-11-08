"""The resume record's rules: its key, which calls may resume, and the window (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2)."""
import pytest

from src.domain.voice_mode import VoiceMode
from src.domain.voice_resume import BUTTON_END_TTL_S, RESUME_CONFIG_FIELD, RESUME_PROVIDER, RESUME_TTL_S, resume_applies, resume_key


def test_the_record_is_keyed_by_user():
    assert resume_key("usr-1") == "voice_resume:usr-1"
    assert resume_key("usr-1") != resume_key("usr-2")


def test_the_window_is_25_minutes_and_the_config_field_is_resume():
    assert RESUME_TTL_S == 25 * 60
    assert RESUME_CONFIG_FIELD == "resume"
    assert BUTTON_END_TTL_S > 0


@pytest.mark.parametrize("mode", [m.value for m in VoiceMode])
def test_every_known_mode_on_xai_applies(mode):
    assert resume_applies(mode, RESUME_PROVIDER) is True


@pytest.mark.parametrize("mode, provider", [("lelik", "openai"), ("lelik", None), ("unknown-mode", "xai"), (None, "xai")])
def test_another_provider_or_an_unknown_mode_does_not_apply(mode, provider):
    assert resume_applies(mode, provider) is False
