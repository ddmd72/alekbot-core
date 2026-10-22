"""tests/plugins/suite_lock.py: one full suite at a time on the machine."""

import json
import os
import stat
import threading
import types

import pytest

from tests.plugins import suite_lock
from tests.support.plugin_runs import isolate_switches, make_importable_for_children, plugin_conftest

pytest_plugins = ["pytester"]

THREE_TESTS = "def test_a():\n    pass\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n"


@pytest.fixture(autouse=True)
def _lock_env(monkeypatch, tmp_path):
    isolate_switches(monkeypatch)
    monkeypatch.delenv("PYTEST_SUITE_LOCK")
    monkeypatch.setenv("PYTEST_SUITE_LOCK_FILE", str(tmp_path / "suite.lock"))
    monkeypatch.setenv("PYTEST_LOCK_TIMEOUT_S", "0.4")
    monkeypatch.setenv("PYTEST_LOCK_REPORT_S", "0.1")
    monkeypatch.setenv("PYTEST_LOCK_POLL_S", "0.02")
    monkeypatch.setattr(suite_lock, "LARGE_SESSION_ITEMS", 2)


@pytest.fixture
def holder(tmp_path):
    """Another session holding the lock (a second descriptor conflicts with the first even in one process)."""
    lock = suite_lock.SuiteLock(str(tmp_path / "suite.lock"))
    lock.acquire(1, 1, 0.01, print)
    yield lock
    lock.release()


def _run(pytester, source=THREE_TESTS, *args):
    pytester.makeconftest(plugin_conftest("suite_lock"))
    pytester.makepyfile(test_inner=source)
    return pytester.runpytest("-p", "no:cacheprovider", *args)


def _everything(result):
    return result.stdout.str() + result.stderr.str()


@pytest.mark.slow
def test_a_large_session_waits_then_gives_up_naming_the_holder(pytester, holder):
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.INTERRUPTED
    out = _everything(result)
    assert f"pid {os.getpid()}" in out
    assert "waiting for another test session" in out
    assert "PYTEST_SUITE_LOCK=0" in out
    assert "passed" not in out


def test_a_large_session_runs_once_the_holder_lets_go(pytester, holder, monkeypatch):
    monkeypatch.setenv("PYTEST_LOCK_TIMEOUT_S", "5")
    threading.Timer(0.3, holder.release).start()
    result = _run(pytester)
    result.assert_outcomes(passed=3)
    assert "waiting for another test session" in _everything(result)


def test_a_small_session_takes_no_lock(pytester, holder, monkeypatch):
    monkeypatch.setattr(suite_lock, "LARGE_SESSION_ITEMS", 100)
    _run(pytester).assert_outcomes(passed=3)


@pytest.mark.slow
@pytest.mark.datafile
def test_n_takes_the_lock_at_configure_before_any_collection(pytester, holder, monkeypatch):
    # Exit raised from pytest_configure is not survivable in pytester's in-process mode, so this one is a real child.
    make_importable_for_children(monkeypatch)
    pytester.makeconftest(plugin_conftest("suite_lock"))
    pytester.makepyfile(test_inner=THREE_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-n", "2")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    assert "collected" not in result.stdout.str()
    assert "waiting for another test session" in _everything(result)
    assert f"pid {os.getpid()}" in _everything(result)


def test_n_zero_is_not_a_large_session(pytester, holder, monkeypatch):
    monkeypatch.setattr(suite_lock, "LARGE_SESSION_ITEMS", 100)
    _run(pytester, THREE_TESTS, "-n", "0").assert_outcomes(passed=3)


def test_the_switch_turns_the_lock_off(pytester, holder, monkeypatch):
    monkeypatch.setenv("PYTEST_SUITE_LOCK", "0")
    _run(pytester).assert_outcomes(passed=3)


def test_a_worker_takes_no_lock():
    registered = []
    config = types.SimpleNamespace(
        workerinput={}, pluginmanager=types.SimpleNamespace(register=lambda *a, **k: registered.append(a))
    )
    suite_lock.pytest_configure(config)
    assert registered == []


def test_the_lock_is_released_when_the_session_ends(pytester, tmp_path):
    _run(pytester).assert_outcomes(passed=3)
    again = suite_lock.SuiteLock(str(tmp_path / "suite.lock"))
    again.acquire(0.2, 1, 0.01, print)
    again.release()


def test_the_holder_writes_its_pid_cwd_and_start_time(tmp_path):
    lock = suite_lock.SuiteLock(str(tmp_path / "x.lock"))
    lock.acquire(1, 1, 0.01, print)
    info = json.loads((tmp_path / "x.lock").read_text())
    assert info["pid"] == os.getpid()
    assert info["cwd"] == os.getcwd()
    assert info["started"] > 0
    lock.release()
    assert (tmp_path / "x.lock").read_text() == ""


def test_the_lock_file_is_world_writable(tmp_path):
    lock = suite_lock.SuiteLock(str(tmp_path / "x.lock"))
    lock.acquire(1, 1, 0.01, print)
    assert stat.S_IMODE(os.stat(tmp_path / "x.lock").st_mode) == 0o666
    lock.release()


def test_a_file_the_session_may_not_write_still_locks_read_only(tmp_path, monkeypatch):
    real_open = os.open

    def deny_write(path, flags, mode=0o777):
        if flags & os.O_RDWR and str(path).endswith("x.lock"):
            raise PermissionError(path)
        return real_open(path, flags, mode)

    (tmp_path / "x.lock").write_text("")
    monkeypatch.setattr(suite_lock.os, "open", deny_write)
    first = suite_lock.SuiteLock(str(tmp_path / "x.lock"))
    first.acquire(1, 1, 0.01, print)
    second = suite_lock.SuiteLock(str(tmp_path / "x.lock"))
    with pytest.raises(TimeoutError):
        second.acquire(0.1, 1, 0.01, lambda message: None)
    first.release()


def test_the_default_lock_file_is_in_the_temp_dir(monkeypatch):
    import tempfile

    monkeypatch.delenv("PYTEST_SUITE_LOCK_FILE")
    assert os.path.dirname(suite_lock.lock_path()) == tempfile.gettempdir()


def test_a_timeout_that_is_not_a_number_is_a_usage_error(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_LOCK_TIMEOUT_S", "soon")
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
