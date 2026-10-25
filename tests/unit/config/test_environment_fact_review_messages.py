"""EnvironmentConfig: the memory review's message claims collection (Facts S4 plan §2.2)."""
import pytest

from src.config.environment import EnvironmentConfig


@pytest.mark.parametrize("env,prefix", [("development", "development_"), ("production", ""), ("test", "test_")])
def test_the_review_messages_collection_is_env_prefixed(env, prefix, monkeypatch):
    monkeypatch.setenv("APP_ENV", env)
    assert EnvironmentConfig().fact_review_messages_collection == f"{prefix}fact_review_messages"
