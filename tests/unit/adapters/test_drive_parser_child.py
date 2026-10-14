"""The drive parser's child entry point (USER_DRIVE_INDEXING_RFC §9, §24.3): request on stdin, limits first, result
in a file, memory failures as their own exit code. Run as a real process, the way the extractor starts it."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapters.drive_parser import child
from src.adapters.drive_parser.child import EXIT_BAD_REQUEST, dispatch, is_memory_failure

_ROOT = Path(__file__).resolve().parents[3]


def _run(stdin: bytes):
    return subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.drive_parser.child"],
                          input=stdin, cwd=_ROOT, capture_output=True, timeout=60)


def _request(tmp_path, **fields):
    base = {"out": str(tmp_path / "result.json"), "memory_bytes": 2 * 1024 ** 3, "cpu_s": 30,
            "file_bytes": 64 * 1024 ** 2}
    return json.dumps({**base, **fields}).encode()


@pytest.mark.parametrize("exc,expected", [
    (MemoryError(), True),
    (ImportError("libonnxruntime.so.1: failed to map segment from shared object"), True),
    (OSError(12, "Cannot allocate memory"), True),
    (OSError("mmap: Cannot allocate memory"), True),
    (ImportError("No module named 'striprtf'"), False),
    (OSError(2, "No such file or directory"), False),
    (ValueError("Cannot allocate memory"), False),
])
def test_is_memory_failure(exc, expected):
    assert is_memory_failure(exc) is expected


def test_unknown_op_is_an_error_result():
    assert dispatch({"op": "nope"})["status"] == "error"


def test_unreadable_request_exits_bad_request(tmp_path):
    proc = _run(b"{not json")
    assert proc.returncode == EXIT_BAD_REQUEST
    assert not (tmp_path / "result.json").exists()


def test_limits_it_cannot_apply_exit_bad_request(tmp_path):
    proc = _run(_request(tmp_path, op="extract", path="x", kind="text", cpu_s=-1))
    assert proc.returncode == EXIT_BAD_REQUEST


def test_cpu_limit_is_applied_before_the_handler(tmp_path):
    code = ("import resource, sys\n"
            "from src.adapters.drive_parser.child import serve\n"
            "def handler(request):\n"
            "    soft, hard = resource.getrlimit(resource.RLIMIT_CPU)\n"
            "    fsize = resource.getrlimit(resource.RLIMIT_FSIZE)[0]\n"
            "    core = resource.getrlimit(resource.RLIMIT_CORE)[1]\n"
            "    return {'soft': soft, 'hard': hard, 'fsize': fsize, 'core': core}\n"
            "sys.exit(serve(handler))\n")
    proc = subprocess.run([sys.executable, "-c", code], input=_request(tmp_path, cpu_s=7, file_bytes=12345),
                          cwd=_ROOT, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    limits = json.loads((tmp_path / "result.json").read_text())
    assert limits == {"soft": 7, "hard": 7 + child.CPU_GRACE_S, "fsize": 12345, "core": 0}
