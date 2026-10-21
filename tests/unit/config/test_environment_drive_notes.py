"""EnvironmentConfig collection names of the drive-note store (DRIVE_FILE_NOTES_RFC §3.6, §4.8)."""
import pytest

from src.config.environment import EnvironmentConfig


@pytest.mark.parametrize("env,prefix", [("development", "development_"), ("production", ""), ("test", "test_")])
def test_names_are_env_prefixed(env, prefix, monkeypatch):
    monkeypatch.setenv("APP_ENV", env)
    cfg = EnvironmentConfig()
    assert cfg.drive_notes_collection == f"{prefix}drive_notes"
    assert cfg.drive_note_erase_requests_collection == f"{prefix}drive_note_erase_requests"
