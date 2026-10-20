import os

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.ports.llm_port import LLMPort
from src.ports.repository import FactRepository
from src.config.environment import EnvironmentConfig, Environment


@pytest.fixture
def mock_env_config():
    config = MagicMock(spec=EnvironmentConfig)
    config.env = Environment.TEST
    config.is_production = False
    config.is_development = False
    config.is_test = True
    config.firestore_collection_prefix = "test_"
    return config


@pytest.fixture
def mock_llm_port():
    return AsyncMock(spec=LLMPort)


@pytest.fixture
def mock_repository():
    return AsyncMock(spec=FactRepository)


def pytest_collection_modifyitems(config, items):
    """`cpu_benchmark` tests are DISABLED by owner decision (2026-10-20): they time CPU work and fail on a
    loaded machine with no code change, so every full run failed and was re-run. Opt back in with
    RUN_CPU_BENCHMARKS=1."""
    if os.getenv("RUN_CPU_BENCHMARKS") == "1":
        return
    skip = pytest.mark.skip(reason="cpu_benchmark disabled (owner, 2026-10-20); set RUN_CPU_BENCHMARKS=1 to run")
    for item in items:
        if "cpu_benchmark" in item.keywords:
            item.add_marker(skip)
