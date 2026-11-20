"""EnvironmentConfig names the skill drafts collection. FirestoreSkillRepository itself: test_skill_store_kit.py."""
from src.config.environment import EnvironmentConfig


def test_env_skill_drafts_collection_is_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    cfg = EnvironmentConfig()
    assert cfg.skill_drafts_collection == f"{cfg.firestore_collection_prefix}skill_drafts"
