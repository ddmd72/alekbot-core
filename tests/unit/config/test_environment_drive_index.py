"""EnvironmentConfig collection names of the drive index (USER_DRIVE_INDEXING_RFC §12.5)."""
import pytest

from src.config.environment import EnvironmentConfig


class TestCollections:
    @pytest.mark.parametrize("env,prefix", [("development", "development_"), ("production", ""), ("test", "test_")])
    def test_names_are_env_prefixed(self, env, prefix, monkeypatch):
        monkeypatch.setenv("APP_ENV", env)
        cfg = EnvironmentConfig()
        assert cfg.drive_index_records_collection == f"{prefix}drive_index_records"
        assert cfg.drive_sync_state_collection == f"{prefix}drive_sync_state"
