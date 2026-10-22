import os

import pytest

# Test-infrastructure plugins (tests/plugins/): per-test CI duration budget, data-file audit, one-suite-at-a-time lock.
# Each is inert unless its env switch is set (see the module docstrings and tests/CLAUDE.md).
pytest_plugins = [
    "tests.plugins.duration_budget",
    "tests.plugins.datafile_audit",
    "tests.plugins.suite_lock",
]
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


def pytest_sessionstart(session):
    """Pay the heavy lazy imports once, before any test. Which test would pay them otherwise depends on how xdist hands
    tests to workers, so the same test passed or broke the CI per-test budget from run to run. Runs in every worker and
    in a plain run (not in the xdist controller, which imports no test code). Importing markitdown here changes nothing
    the tests can see: the laziness assertions run in subprocesses."""
    config = session.config
    if getattr(config.option, "numprocesses", None) and not hasattr(config, "workerinput"):
        return
    try:
        import markitdown  # noqa: F401
        import markitdown.converters  # noqa: F401
    except ImportError:  # optional dependency
        pass


def pytest_collection_modifyitems(config, items):
    """`timing` tests (wall-clock or CPU-time assertions) are off every everyday run by owner decision
    (2026-10-20): they fail on a loaded machine with no code change, so full runs failed and were re-run.
    They run only via `make test-timing` (RUN_TIMING_TESTS=1) — in CI after a merge to main, or by hand.

    `slow` tests (over the 1 s per-test CI budget) are off the everyday run the same way and run in `make ci`
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
