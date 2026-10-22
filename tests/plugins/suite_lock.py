"""One full suite at a time on the machine (TEST_COVERAGE_SYSTEMIC_RFC §4b).

Five parallel full suites on one box turned a 1:34 run into 13:24 (contention, not the suite). A large session takes
an exclusive `fcntl.flock` on a lock file in the temp dir, in `pytest_configure` of the controller only (never an
xdist worker), and waits while another session holds it, printing the holder (pid, cwd, start time, command) every
30 s. The kernel releases the lock when the process dies; it is released explicitly in `pytest_unconfigure`.

Large = `-n` given, or a collection of at least LARGE_SESSION_ITEMS tests (checked at collection finish).
Env: `PYTEST_SUITE_LOCK=0` disables it; `PYTEST_LOCK_TIMEOUT_S` (default 1800) is the longest wait.
A lock file the user cannot open at all (owned by another user, mode 0600) is reported and the run goes on without it.
Test knobs: `PYTEST_SUITE_LOCK_FILE` (lock path), `PYTEST_LOCK_REPORT_S` (default 30), `PYTEST_LOCK_POLL_S` (default 0.5).
"""

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from typing import Optional

import pytest

LARGE_SESSION_ITEMS = 2000
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_REPORT_S = 30.0
DEFAULT_POLL_S = 0.5


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise pytest.UsageError(f"{name}={raw!r} is not a number of seconds") from None


def lock_path() -> str:
    return os.environ.get("PYTEST_SUITE_LOCK_FILE") or os.path.join(tempfile.gettempdir(), "alekbot-pytest-suite.lock")


class SuiteLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def _open(self) -> int:
        # World-writable: root and another user share one temp dir on the cloud VM.
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        except PermissionError:
            return os.open(self.path, os.O_RDONLY)  # flock works on a read-only descriptor
        with contextlib.suppress(PermissionError, OSError):  # an existing file owned by another user
            os.fchmod(fd, 0o666)
        return fd

    def holder(self) -> str:
        try:
            with open(self.path, encoding="utf-8") as handle:
                info = json.loads(handle.read() or "{}")
        except (OSError, ValueError):
            return "unknown holder"
        started = info.get("started")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "?"
        return f"pid {info.get('pid', '?')}, cwd {info.get('cwd', '?')}, started {when}, command: {info.get('argv', '?')}"

    def acquire(self, timeout_s: float, report_s: float, poll_s: float, say) -> None:
        fd = self._open()
        started = time.monotonic()
        next_report = started
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if now - started >= timeout_s:
                    os.close(fd)
                    raise TimeoutError(
                        f"another full test session held {self.path} for {timeout_s:g} s: {self.holder()}. "
                        "Wait for it, or set PYTEST_SUITE_LOCK=0 to run anyway."
                    ) from None
                if now >= next_report:
                    say(f"[suite lock] waiting for another test session ({now - started:.0f} s): {self.holder()}")
                    next_report = now + report_s
                time.sleep(poll_s)
        self._fd = fd
        info = json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "started": time.time(), "argv": " ".join(sys.argv)})
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.pwrite(fd, info.encode("utf-8"), 0)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self._fd, 0)
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


class SuiteLockPlugin:
    def __init__(self) -> None:
        self.lock = SuiteLock(lock_path())

    def take(self, config: pytest.Config) -> None:
        if self.lock.held:
            return
        capman = config.pluginmanager.getplugin("capturemanager")

        def say(message: str) -> None:
            if capman is not None:
                with capman.global_and_fixture_disabled():
                    sys.stderr.write(message + "\n")
                    sys.stderr.flush()
            else:
                sys.stderr.write(message + "\n")
                sys.stderr.flush()

        try:
            self.lock.acquire(
                _float_env("PYTEST_LOCK_TIMEOUT_S", DEFAULT_TIMEOUT_S),
                _float_env("PYTEST_LOCK_REPORT_S", DEFAULT_REPORT_S),
                _float_env("PYTEST_LOCK_POLL_S", DEFAULT_POLL_S),
                say,
            )
        except PermissionError as error:
            # A lock file owned by another user with mode 0600 can be neither written nor locked: run without it.
            say(f"[suite lock] cannot open {self.lock.path} ({error.strerror or error}); running without the lock")
        except TimeoutError as error:
            pytest.exit(str(error), returncode=int(pytest.ExitCode.INTERRUPTED))

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        if len(session.items) >= LARGE_SESSION_ITEMS:
            self.take(session.config)

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        self.lock.release()


def _is_xdist_session(config: pytest.Config) -> bool:
    return bool(getattr(config.option, "numprocesses", None)) and getattr(config.option, "dist", "no") != "no"


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("PYTEST_SUITE_LOCK") == "0" or hasattr(config, "workerinput"):
        return
    plugin = SuiteLockPlugin()
    config.pluginmanager.register(plugin, "suite-lock")
    if _is_xdist_session(config):
        plugin.take(config)
