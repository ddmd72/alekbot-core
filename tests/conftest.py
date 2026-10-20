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
    """`timing` tests (wall-clock or CPU-time assertions) are off every everyday run by owner decision
    (2026-10-20): they fail on a loaded machine with no code change, so full runs failed and were re-run.
    They run only via `make test-timing` (RUN_TIMING_TESTS=1) — in CI after a merge to main, or by hand."""
    if os.getenv("RUN_TIMING_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="timing test: run with `make test-timing` (RUN_TIMING_TESTS=1)")
    for item in items:
        if "timing" in item.keywords:
            item.add_marker(skip)
