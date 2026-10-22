"""Helpers for the tests of tests/plugins/* and the tests/conftest.py hooks: they run an inner pytest through
`pytester` and must not inherit the switches the outer run (`make ci`) sets."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SWITCHES = (
    "PYTEST_DURATION_BUDGET_S",
    "PYTEST_DATAFILE_AUDIT",
    "PYTEST_SUITE_LOCK",
    "PYTEST_SUITE_LOCK_FILE",
    "PYTEST_LOCK_TIMEOUT_S",
    "PYTEST_LOCK_REPORT_S",
    "PYTEST_LOCK_POLL_S",
    "RUN_SLOW_TESTS",
    "RUN_TIMING_TESTS",
)


def isolate_switches(monkeypatch) -> None:
    """Inner runs start with every switch unset, and no suite lock (the outer session may hold the real one)."""
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTEST_SUITE_LOCK", "0")


def make_importable_for_children(monkeypatch) -> None:
    """Child pytest processes (xdist workers, `runpytest_subprocess`) import `tests.plugins.*` from the repo."""
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(REPO_ROOT), existing])))


def plugin_conftest(*modules: str) -> str:
    names = ", ".join(repr(f"tests.plugins.{m}") for m in modules)
    return f"pytest_plugins = [{names}]\n"
