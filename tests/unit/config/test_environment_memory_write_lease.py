"""EnvironmentConfig collection name of the memory-write lease (FACT_LIFECYCLE_RFC D9)."""
import pytest

from src.config.environment import EnvironmentConfig


@pytest.mark.parametrize("env,prefix", [("development", "development_"), ("production", ""), ("test", "test_")])
def test_the_lease_collection_is_env_prefixed(env, prefix, monkeypatch):
    monkeypatch.setenv("APP_ENV", env)
    assert EnvironmentConfig().memory_write_leases_collection == f"{prefix}memory_write_leases"
