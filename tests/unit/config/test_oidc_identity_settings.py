"""IAM least-privilege Step 0 (C1): the identity emails are real `load_settings()` keys and are stripped
(the `config.get()` trap: an unlisted key silently resolves to None)."""
import pytest

from src.config import settings as settings_module

_KEYS = ("SERVICE_ACCOUNT_EMAIL", "DRIVE_INVOKER_SA_EMAIL", "VOICE_RELAY_SA_EMAIL")


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)  # no Secret Manager lookups
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OIDC_TRANSITION_SA_EMAILS", raising=False)
    return monkeypatch


def test_new_keys_exist_and_are_none_when_unset(clean_env):
    config = settings_module.load_settings()
    for key in _KEYS:
        assert key in config and config[key] is None


@pytest.mark.parametrize("key", _KEYS)
def test_values_are_stripped(clean_env, key):
    clean_env.setenv(key, " sa@example.invalid\n")
    assert settings_module.load_settings()[key] == "sa@example.invalid"


def test_blank_value_becomes_none(clean_env):
    clean_env.setenv("DRIVE_INVOKER_SA_EMAIL", "  \n")
    assert settings_module.load_settings()["DRIVE_INVOKER_SA_EMAIL"] is None


def test_transition_list_is_an_optional_knob_not_a_setting(clean_env):
    assert "OIDC_TRANSITION_SA_EMAILS" not in settings_module.load_settings()
