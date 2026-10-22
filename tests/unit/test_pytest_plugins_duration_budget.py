"""tests/plugins/duration_budget.py: the per-test CI budget (PYTEST_DURATION_BUDGET_S)."""

import pytest

from tests.support.plugin_runs import isolate_switches, make_importable_for_children, plugin_conftest

pytest_plugins = ["pytester"]

SLEEPY = """
import time
import pytest

def test_sleepy():
    time.sleep(0.12)

def test_quick():
    pass
"""


@pytest.fixture(autouse=True)
def _clean_switches(monkeypatch):
    isolate_switches(monkeypatch)


def _run(pytester, source, *args):
    pytester.makeconftest(plugin_conftest("duration_budget"))
    pytester.makepyfile(test_inner=source)
    return pytester.runpytest("-p", "no:cacheprovider", *args)


def test_a_test_over_the_budget_fails_the_run_though_it_passed(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    result = _run(pytester, SLEEPY)
    result.assert_outcomes(passed=2)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = result.stdout.str()
    assert "test_inner.py::test_sleepy" in out
    assert "test_inner.py::test_quick" not in out
    assert "0.05 s budget (1)" in out


def test_the_listing_is_slowest_first(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    source = "import time\n\ndef test_a():\n    time.sleep(0.1)\n\ndef test_b():\n    time.sleep(0.2)\n"
    out = _run(pytester, source).stdout.str()
    assert out.index("test_b") < out.index("test_a")


def test_slow_and_timing_marked_tests_are_exempt(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    source = (
        "import time\nimport pytest\n\n"
        "@pytest.mark.slow\ndef test_slow():\n    time.sleep(0.12)\n\n"
        "@pytest.mark.timing\ndef test_timing():\n    time.sleep(0.12)\n\n"
        "class TestGroup:\n    pytestmark = pytest.mark.slow\n\n    def test_in_a_slow_class(self):\n        time.sleep(0.12)\n"
    )
    result = _run(pytester, source)
    result.assert_outcomes(passed=3)
    assert result.ret == pytest.ExitCode.OK


def test_setup_and_teardown_count_towards_the_budget(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.1")
    source = (
        "import time\nimport pytest\n\n"
        "@pytest.fixture\ndef heavy():\n    time.sleep(0.06)\n    yield\n    time.sleep(0.06)\n\n"
        "def test_cheap_body(heavy):\n    pass\n"
    )
    result = _run(pytester, source)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "test_cheap_body" in result.stdout.str()


def test_without_the_env_var_the_plugin_does_nothing(pytester):
    result = _run(pytester, SLEEPY)
    result.assert_outcomes(passed=2)
    assert result.ret == pytest.ExitCode.OK
    assert "budget" not in result.stdout.str()


def test_a_run_within_the_budget_passes(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "5")
    result = _run(pytester, SLEEPY)
    assert result.ret == pytest.ExitCode.OK


def test_a_failing_run_keeps_its_own_exit_status(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    result = _run(pytester, "import time\n\ndef test_fails():\n    time.sleep(0.12)\n    assert False\n")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "budget" in result.stdout.str()


def test_a_budget_that_is_not_a_number_is_a_usage_error(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "fast")
    result = _run(pytester, SLEEPY)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "not a number of seconds" in result.stderr.str() + result.stdout.str()


@pytest.mark.slow
@pytest.mark.datafile
def test_under_xdist_the_controller_sums_the_workers_reports(pytester, monkeypatch):
    make_importable_for_children(monkeypatch)
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    result = _run(pytester, SLEEPY, "-n", "2")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    out = result.stdout.str()
    assert "test_inner.py::test_sleepy" in out
    assert "0.05 s budget (1)" in out
