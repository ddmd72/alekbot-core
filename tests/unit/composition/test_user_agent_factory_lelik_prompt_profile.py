"""LELIK_PROMPT_PROFILE: an optional env knob read in composition and injected into LelikAgent."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.user import UserBotConfig


def _build():
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid")
    ctx = _UserContext(user_profile=profile, prompt_builder=MagicMock())
    fake = SimpleNamespace(
        _telephony=MagicMock(),
        config={"TWILIO_PHONE_NUMBER": "+346001", "CLOUD_RUN_SERVICE_URL": "https://main.example.com"},
        notification_service=MagicMock(),
        repository=MagicMock(),
        session_store=MagicMock(),
    )
    return UserAgentFactory._build_lelik(fake, "u1", ctx)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_blank_knob_keeps_the_lelik_profile(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LELIK_PROMPT_PROFILE", raising=False)
    else:
        monkeypatch.setenv("LELIK_PROMPT_PROFILE", value)
    assert _build()._prompt_profile == "lelik"


def test_knob_selects_the_profile(monkeypatch):
    monkeypatch.setenv("LELIK_PROMPT_PROFILE", "lelik_bare")
    assert _build()._prompt_profile == "lelik_bare"
