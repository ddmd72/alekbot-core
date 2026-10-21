"""
Launcher helpers for the media child (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.4, Δ99): the same contract as
`SubprocessContentExtractor`'s — a fresh child per op, limits applied by `child.serve` before any import, an allowlisted
environment, a private directory owned by the parser user, a process-group kill on the wall, a stderr digest instead of
text. DUPLICATED on purpose (plan G15): REQ-ARCH-08 keeps the top-level extractor from importing `drive_parser/`, and its
tests patch its module internals; the two converge when the parsers move to a zero-credential service.

The request, URL included, travels on the child's stdin only: never in this process's argv, never logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import pwd
import signal
import stat
import sys
from typing import IO, Any, Dict, Mapping, Optional, Tuple

from src.domain.drive_extraction import text_digest
from src.domain.memory_telemetry import SCOPE_SUBPROCESS
from src.utils.logger import logger
from src.utils.memory_scope import memory_scope

CHILD_MODULE = "src.adapters.drive_parser.child"
EXIT_MEMORY = 3                  # mirrored from child.py (a test pins them in step)
EXIT_BAD_REQUEST = 4
PARSER_USER_ENV = "DRIVE_PARSER_USER"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")
_FIXED_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2"}
_STDERR_TAIL = 400
RESULT_MAX_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 60 * 1024    # the child reads at most 64 KiB of stdin


def child_env(workdir: str, *, sandboxed: bool = False) -> Dict[str, str]:
    """Allowlisted: no credentials, no PYTHON* variables; TMPDIR is the call's own directory, removed after."""
    env = {key: value for key in _SAFE_ENV_KEYS if (value := os.environ.get(key))}
    env.update(_FIXED_ENV)
    env["TMPDIR"] = workdir
    if sandboxed:
        env["HOME"] = workdir
    return env


def _effective_uid() -> int:
    return os.geteuid()


def resolve_sandbox(user: Optional[str]) -> Optional[Tuple[int, int]]:
    """(uid, gid) of the parser user, or None when none is configured. A configured user that is missing, is root, or
    that this process cannot switch to fails the start: never a silent fallback (R3 F3-1)."""
    user = (user or "").strip()
    if not user:
        logger.warning(f"[DriveMedia] no parser user ({PARSER_USER_ENV} unset): media children run as this process's "
                       f"own user, with its privileges — local development only; the indexer image sets it")
        return None
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        logger.error(f"[DriveMedia] parser user {user!r} does not exist in this container")
        raise RuntimeError(f"parser user {user!r} does not exist in this container") from None
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        logger.error(f"[DriveMedia] parser user {user!r} is root")
        raise RuntimeError(f"parser user {user!r} is root: no sandbox")
    if _effective_uid() != 0:
        logger.error(f"[DriveMedia] switching the media child to {user!r} needs root")
        raise RuntimeError(f"running the media child as {user!r} needs root")
    logger.info(f"[DriveMedia] media children run as {user} (uid {entry.pw_uid}, gid {entry.pw_gid})")
    return entry.pw_uid, entry.pw_gid


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        logger.debug(f"[DriveMedia] child group {pid} already gone: {type(exc).__name__}")


async def spawn(request: Mapping[str, Any], workdir: str, stderr: IO[bytes], *, python: str, module: str,
                wall_s: float, sandbox: Optional[Tuple[int, int]]) -> Tuple[int, bool]:
    """(return code, timed_out). The whole process group dies at the wall: the ffmpeg grandchild with it."""
    with memory_scope(SCOPE_SUBPROCESS, "drive_media"):
        identity: Dict[str, Any] = {}
        if sandbox is not None:   # no supplementary groups: root's would otherwise be inherited
            identity = {"user": sandbox[0], "group": sandbox[1], "extra_groups": []}
        proc = await asyncio.create_subprocess_exec(
            python, "-B", "-E", "-s", "-m", module,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=stderr,
            env=child_env(workdir, sandboxed=sandbox is not None), cwd=_REPO_ROOT, start_new_session=True, **identity)
        timed_out = False
        try:
            await asyncio.wait_for(proc.communicate(json.dumps(request).encode("utf-8")), timeout=wall_s)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            # Always: a child that exited 0 can leave an ffmpeg grandchild in its group. A group id with no member
            # left is unsignalable, so the late call is harmless.
            kill_group(proc.pid)
            if proc.returncode is None:
                await proc.wait()
        return int(proc.returncode if proc.returncode is not None else -signal.SIGKILL), timed_out


def exit_status(returncode: int, timed_out: bool, wall_s: float, cpu_s: int) -> Tuple[str, str]:
    """("", "") when the child exited 0; else (kind, detail), kind one of `timeout`, `memory`, `error`."""
    if timed_out:
        return "timeout", f"wall clock {wall_s:g}s"
    if returncode == 0:
        return "", ""
    if returncode == EXIT_MEMORY:
        return "memory", "MemoryError"
    if returncode == -signal.SIGXCPU:
        return "timeout", f"cpu limit {cpu_s}s"
    if returncode == -signal.SIGKILL:
        return "memory", "killed"
    if returncode == EXIT_BAD_REQUEST:
        return "error", "child refused the request"
    return "error", f"exit {returncode}"


def load_result(path: str) -> Optional[Dict[str, Any]]:
    """The child's result: a regular file only, opened without following a link and without blocking on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > RESULT_MAX_BYTES:
            return None
        with os.fdopen(fd, encoding="utf-8", closefd=False) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def collect_window(written: str, wanted: str, max_bytes: int) -> bool:
    """Copies one window the child wrote to where the caller wants it. Only a regular file within `max_bytes` moves,
    read through one descriptor opened without following a link or blocking on a FIFO (the child owns the directory
    and could plant either). Anything else is refused."""
    try:
        fd = os.open(written, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    partial = wanted + ".part"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes or info.st_size == 0:
            return False
        copied = 0
        with open(partial, "wb") as out:
            while chunk := os.read(fd, 1024 * 1024):
                copied += len(chunk)
                if copied > max_bytes:
                    break
                out.write(chunk)
        if copied > max_bytes:
            os.unlink(partial)
            return False
        os.replace(partial, wanted)
        return True
    except OSError as exc:
        logger.warning(f"[DriveMedia] window not collected: {type(exc).__name__}")
        if exc.errno != errno.ENOENT:
            with contextlib.suppress(OSError):
                os.unlink(partial)
        return False
    finally:
        os.close(fd)


def stderr_digest(stderr: IO[bytes]) -> str:
    """`stderr <n> bytes #<hash of its tail>`; never the text (ffmpeg's errors quote the URL and the file)."""
    try:
        size = os.fstat(stderr.fileno()).st_size
        stderr.seek(max(0, size - _STDERR_TAIL))
        return f"stderr {size} bytes #{text_digest(stderr.read())}" if size else "stderr empty"
    except OSError:
        return "stderr unreadable"


def default_python() -> str:
    return sys.executable
