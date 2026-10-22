"""The `slow` skip hook in tests/conftest.py (RUN_SLOW_TESTS), next to the `timing` one."""

import pytest

from tests.support.plugin_runs import isolate_switches

pytest_plugins = ["pytester"]

SOURCE = """
import pytest

@pytest.mark.slow
def test_slow():
    pass

@pytest.mark.timing
def test_timing():
    pass

def test_plain():
    pass

class TestGroup:
    pytestmark = pytest.mark.slow

    def test_in_a_slow_class(self):
        pass
"""


@pytest.fixture(autouse=True)
def _clean_switches(monkeypatch):
    isolate_switches(monkeypatch)


def _run(pytester):
    pytester.makeconftest("from tests.conftest import pytest_collection_modifyitems  # noqa: F401\n")
    pytester.makepyfile(test_inner=SOURCE)
    return pytester.runpytest("-p", "no:cacheprovider", "-rs")


def test_slow_and_timing_tests_are_skipped_by_default(pytester):
    result = _run(pytester)
    result.assert_outcomes(passed=1, skipped=3)
    out = result.stdout.str()
    assert "RUN_SLOW_TESTS=1" in out
    assert "RUN_TIMING_TESTS=1" in out


def test_run_slow_tests_runs_the_slow_ones_and_keeps_timing_skipped(pytester, monkeypatch):
    monkeypatch.setenv("RUN_SLOW_TESTS", "1")
    _run(pytester).assert_outcomes(passed=3, skipped=1)


def test_run_timing_tests_runs_the_timing_one_and_keeps_slow_skipped(pytester, monkeypatch):
    monkeypatch.setenv("RUN_TIMING_TESTS", "1")
    _run(pytester).assert_outcomes(passed=2, skipped=2)


def test_both_switches_run_everything(pytester, monkeypatch):
    monkeypatch.setenv("RUN_SLOW_TESTS", "1")
    monkeypatch.setenv("RUN_TIMING_TESTS", "1")
    _run(pytester).assert_outcomes(passed=4)


@pytest.mark.datafile
def test_the_markers_are_registered_in_pytest_ini(pytester):
    import configparser

    from tests.support.plugin_runs import REPO_ROOT

    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    markers = parser["pytest"]["markers"]
    for name in ("slow:", "datafile:", "timing:"):
        assert name in markers
