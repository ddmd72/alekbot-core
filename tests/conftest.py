import os

import pytest

# Test-infrastructure plugin (tests/plugins/): one full suite at a time (see the module docstring and tests/CLAUDE.md).
pytest_plugins = [
    "tests.plugins.suite_lock",
]
from unittest.mock import AsyncMock, MagicMock
from src.ports.llm_port import LLMPort
from src.ports.repository import FactRepository
from src.config.environment import EnvironmentConfig, Environment


@pytest.fixture(scope="session", autouse=True)
def _eval_transactions_everywhere():
    """Every `@firestore.async_transactional` runs the evaluating fake's loop for a transaction of `EvalDb`, in every test;
    any other transaction reaches the decorator that was installed before (tests/support/firestore_eval.py)."""
    from tests.support.firestore_eval import dispatching_transactions

    with dispatching_transactions():
        yield


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
    They run only via `make test-timing` (RUN_TIMING_TESTS=1) — in CI after a merge to main, or by hand.

    `slow` tests (over 0.5 s with coverage on) are off the everyday run the same way and run in `make ci`
    (RUN_SLOW_TESTS=1), which sets the switch."""
    skips = []
    if os.getenv("RUN_TIMING_TESTS") != "1":
        skips.append(
            ("timing", pytest.mark.skip(reason="timing test: run with `make test-timing` (RUN_TIMING_TESTS=1)"))
        )
    if os.getenv("RUN_SLOW_TESTS") != "1":
        skips.append(("slow", pytest.mark.skip(reason="slow test: run with `make ci` (RUN_SLOW_TESTS=1)")))
    if not skips:
        return
    for item in items:
        for name, skip in skips:
            if item.get_closest_marker(name) is not None:
                item.add_marker(skip)
