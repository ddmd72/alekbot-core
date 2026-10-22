"""limits_exec (RFC §4.6/§4.8, plan G6): the limits are in force before the engine loads."""
import json
import os
import subprocess
import sys

import pytest

from src.adapters.sandbox_engine import limits_exec

pytestmark = pytest.mark.datafile

_LINUX = sys.platform == "linux"
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_REPORT = (
    "import json, os, resource as r;"
    "print(json.dumps({'core': r.getrlimit(r.RLIMIT_CORE), 'cpu': r.getrlimit(r.RLIMIT_CPU),"
    "'fsize': r.getrlimit(r.RLIMIT_FSIZE), 'nofile': r.getrlimit(r.RLIMIT_NOFILE),"
    "'nproc': r.getrlimit(r.RLIMIT_NPROC), 'as': r.getrlimit(r.RLIMIT_AS),"
    "'data': r.getrlimit(r.RLIMIT_DATA), 'cwd': os.getcwd()}))"
)


def _limits(**overrides):
    base = {"memory_bytes": 1 << 30, "memory_mode": "as", "cpu_s": 30, "file_bytes": 1 << 20,
            "nproc": 4000, "nofile": 128}
    base.update(overrides)
    return base


def _run(limits, argv, *, raw=None):
    first = raw if raw is not None else json.dumps(limits)
    return subprocess.run(
        [sys.executable, "-B", "-E", "-s", "-m", "src.adapters.sandbox_engine.limits_exec", first, "--", *argv],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60)


def test_limits_are_in_force_before_the_engine_runs(tmp_path):
    proc = _run(_limits(cwd=str(tmp_path)), [sys.executable, "-c", _REPORT])
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    assert seen["core"] == [0, 0]
    assert seen["cpu"] == [30, 30 + limits_exec.CPU_GRACE_S]
    assert seen["fsize"] == [1 << 20, 1 << 20]
    assert seen["nofile"] == [128, 128]
    assert seen["nproc"] == [4000, 4000]
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(str(tmp_path))


@pytest.mark.skipif(not _LINUX, reason="RLIMIT_AS / RLIMIT_DATA are only honoured on Linux")
def test_memory_mode_as_sets_address_space_only():
    seen = json.loads(_run(_limits(memory_mode="as"), [sys.executable, "-c", _REPORT]).stdout)
    assert seen["as"] == [1 << 30, 1 << 30]
    assert seen["data"][0] != 1 << 30


@pytest.mark.skipif(not _LINUX, reason="RLIMIT_AS / RLIMIT_DATA are only honoured on Linux")
def test_memory_mode_data_sets_data_segment_only():
    seen = json.loads(_run(_limits(memory_mode="data"), [sys.executable, "-c", _REPORT]).stdout)
    assert seen["data"] == [1 << 30, 1 << 30]
    assert seen["as"][0] != 1 << 30


def test_default_memory_mode_is_address_space():
    assert limits_exec.MEMORY_MODE_DEFAULT == "as"   # G6: S0-f decides


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"cpu_s": 1})])
def test_unreadable_limits_exit_bad_request(raw):
    proc = _run(None, [sys.executable, "-c", "print(1)"], raw=raw)
    assert proc.returncode == limits_exec.EXIT_BAD_REQUEST
    assert proc.stdout == ""


def test_unknown_memory_mode_exits_bad_request():
    proc = _run(_limits(memory_mode="swap"), [sys.executable, "-c", "print(1)"])
    assert proc.returncode == limits_exec.EXIT_BAD_REQUEST


def test_missing_separator_exits_bad_request():
    proc = subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.sandbox_engine.limits_exec",
                           json.dumps(_limits()), sys.executable], cwd=_REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == limits_exec.EXIT_BAD_REQUEST


@pytest.mark.skipif(not _LINUX or os.geteuid() == 0,
                    reason="Linux refuses a hard limit above the current one for non-root; macOS accepts it")
def test_a_limit_that_cannot_be_applied_exits_bad_request_and_the_engine_never_runs():
    proc = _run(_limits(nofile=1 << 40), [sys.executable, "-c", "print('engine ran')"])
    assert proc.returncode == limits_exec.EXIT_BAD_REQUEST
    assert "engine ran" not in proc.stdout


def test_an_engine_that_cannot_be_executed_exits_distinctly():
    proc = _run(_limits(), ["/nonexistent/engine-binary"])
    assert proc.returncode == limits_exec.EXIT_EXEC_FAILED


def test_exit_codes_stay_apart_from_the_parser_children_codes():
    assert limits_exec.EXIT_BAD_REQUEST == 4   # same meaning as drive_parser.child
    assert limits_exec.EXIT_EXEC_FAILED not in (0, 3, 4)
