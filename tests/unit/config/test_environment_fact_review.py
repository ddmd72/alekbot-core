"""EnvironmentConfig collection names of the memory review's questions and state (Facts S4 plan §2.2)."""
import pytest

from src.config.environment import EnvironmentConfig


@pytest.mark.parametrize("env,prefix", [("development", "development_"), ("production", ""), ("test", "test_")])
def test_the_review_collections_are_env_prefixed(env, prefix, monkeypatch):
    monkeypatch.setenv("APP_ENV", env)
    config = EnvironmentConfig()
    assert config.fact_review_questions_collection == f"{prefix}fact_review_questions"
    assert config.fact_review_state_collection == f"{prefix}fact_review_state"
