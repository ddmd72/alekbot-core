"""tests/plugins/duration_budget.py: the per-test duration budget (PYTEST_DURATION_BUDGET_S), a warning and never a gate."""

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
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _run(pytester, source, *args):
    pytester.makeconftest(plugin_conftest("duration_budget"))
    pytester.makepyfile(test_inner=source)
    return pytester.runpytest("-p", "no:cacheprovider", *args)


def test_a_test_over_the_budget_is_reported_and_the_exit_status_stays_green(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    result = _run(pytester, SLEEPY)
    result.assert_outcomes(passed=2)
    assert result.ret == pytest.ExitCode.OK
    out = result.stdout.str()
    assert "test_inner.py::test_sleepy" in out
    assert "test_inner.py::test_quick" not in out
    assert "0.05 s budget (1) -- warning only" in out


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
    assert result.ret == pytest.ExitCode.OK
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
    assert result.ret == pytest.ExitCode.OK
    out = result.stdout.str()
    assert "test_inner.py::test_sleepy" in out
    assert "0.05 s budget (1) -- warning only" in out


def test_the_listing_shows_the_setup_call_teardown_split(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    source = (
        "import time\nimport pytest\n\n"
        "@pytest.fixture\ndef heavy():\n    time.sleep(0.12)\n    yield\n\n"
        "def test_setup_heavy(heavy):\n    pass\n"
    )
    out = _run(pytester, source).stdout.str()
    line = next(line for line in out.splitlines() if "test_setup_heavy" in line)
    assert "setup 0.1" in line and "call 0.00" in line and "teardown 0.00" in line


def test_a_parametrize_id_called_slow_is_not_the_marker(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    source = "import time\nimport pytest\n\n@pytest.mark.parametrize('kind', ['slow', 'timing'])\ndef test_sleepy(kind):\n    time.sleep(0.12)\n"
    result = _run(pytester, source)
    assert result.ret == pytest.ExitCode.OK
    assert "0.05 s budget (2)" in result.stdout.str()


def test_a_green_run_says_the_budget_was_checked(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "5")
    result = _run(pytester, SLEEPY)
    assert result.ret == pytest.ExitCode.OK
    assert "duration budget 5 s: 0 over (2 tests)" in result.stdout.str()


def test_a_failing_test_stays_failed_and_the_report_still_comes(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    result = _run(pytester, "import time\n\ndef test_fails():\n    time.sleep(0.12)\n    assert False\n")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "warning only" in result.stdout.str()


def _annotation_lines(result):
    return [line for line in result.stdout.lines if line.startswith("::warning")]


def test_on_github_actions_the_offenders_are_also_one_warning_annotation(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    source = "import time\n\ndef test_a():\n    time.sleep(0.1)\n\ndef test_b():\n    time.sleep(0.2)\n"
    result = _run(pytester, source)

    assert result.ret == pytest.ExitCode.OK
    (line,) = _annotation_lines(result)
    assert line.startswith("::warning title=Tests over the 0.05 s budget::")
    message = line.split("::", 2)[2]
    assert "\n" not in message and message.count("%0A") == 1  # two offenders, one line break
    assert message.index("test_b") < message.index("test_a")  # slowest first


def test_there_is_no_annotation_without_github_actions(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "0.05")
    result = _run(pytester, SLEEPY)
    assert "warning only" in result.stdout.str()
    assert _annotation_lines(result) == []


def test_there_is_no_annotation_on_a_green_run(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DURATION_BUDGET_S", "5")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _annotation_lines(_run(pytester, SLEEPY)) == []


def test_the_annotation_escapes_percent_signs_before_line_breaks():
    from tests.plugins.duration_budget import github_warning

    line = github_warning(1.0, ["1.50s tests/test_x.py::test_a[100%] (call 1.50)", "1.20s tests/test_x.py::test_b (call 1.20)"])
    assert line.startswith("::warning title=Tests over the 1 s budget::")
    assert "test_a[100%25]" in line and "%0A1.20s" in line
    assert "\n" not in line


def test_a_long_list_is_cut_in_the_annotation(pytester):
    from tests.plugins.duration_budget import MAX_ANNOTATED, github_warning

    line = github_warning(1.0, [f"2.00s t{i}" for i in range(MAX_ANNOTATED + 7)])
    assert line.count("%0A") == MAX_ANNOTATED  # MAX lines plus the "... and 7 more" line
    assert line.endswith("... and 7 more")
