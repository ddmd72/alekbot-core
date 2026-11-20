from src.config.environment import EnvironmentConfig


def test_env_config_collection_is_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    cfg = EnvironmentConfig()
    assert cfg.memory_fact_drafts_collection == f"{cfg.firestore_collection_prefix}memory_fact_drafts"
