"""EnvironmentConfig names the skills collection. FirestoreSkillRepository itself: test_skill_store_kit.py."""
from src.config.environment import EnvironmentConfig


def test_env_config_skills_collection_is_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    cfg = EnvironmentConfig()
    assert cfg.skills_collection == f"{cfg.firestore_collection_prefix}skills"
