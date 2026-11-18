"""tests/plugins/skip_report.py: the run's outcomes and skip reasons land in SKIP_REPORT_PATH."""
import json

import pytest

from tests.support.plugin_runs import isolate_switches, plugin_conftest

pytest_plugins = ["pytester"]

SOURCE = """
import pytest

def test_ok():
    pass

@pytest.mark.skipif(True, reason="needs a thing")
def test_skipped_a():
    pass

def test_skipped_b():
    pytest.skip("needs a thing")

def test_skipped_c():
    pytest.skip("another reason")

@pytest.mark.xfail(reason="known")
def test_expected_failure():
    assert False

@pytest.mark.xfail(reason="fixed")
def test_unexpected_pass():
    pass

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

def test_setup_error(broken):
    pass

def test_plain_failure():
    assert False
"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    isolate_switches(monkeypatch)
    monkeypatch.delenv("SKIP_REPORT_PATH", raising=False)


def _run(pytester):
    pytester.makeconftest(plugin_conftest("skip_report"))
    pytester.makepyfile(test_inner=SOURCE)
    return pytester.runpytest("-p", "no:cacheprovider")


def test_the_report_counts_outcomes_and_groups_skips_by_reason(pytester, monkeypatch, tmp_path):
    out = tmp_path / "report.json"
    monkeypatch.setenv("SKIP_REPORT_PATH", str(out))
    _run(pytester)
    report = json.loads(out.read_text())
    assert report["outcomes"] == {"passed": 1, "skipped": 3, "xfailed": 1, "xpassed": 1, "error": 1, "failed": 1}
    assert report["skipped_by_reason"]["needs a thing"]["count"] == 2
    assert report["skipped_by_reason"]["another reason"]["count"] == 1
    assert all("test_inner.py::" in t for t in report["skipped_by_reason"]["needs a thing"]["tests"])


def test_without_the_path_nothing_is_written(pytester, tmp_path):
    _run(pytester)
    assert not list(tmp_path.iterdir())
