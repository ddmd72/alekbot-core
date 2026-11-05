"""
HtmlRenderEngine: the service's `render.pdf` / `render.png` engine (UNTRUSTED_CONTENT_SANDBOX_RFC §4.5-§4.8; slice S2).
One instance per op (the `ParseEngine(op=...)` pattern):

    HtmlRenderEngine(launcher, *, op, output_root, browser_cache_dir, chromium_sandbox=CHROMIUM_OWN_SANDBOX)

Each request runs `render_html_child` through the `ChildLauncher` (own uid, allowlisted environment, `limits_exec` applied
before the child loads); the child validates the body and execs the fixed `pdf_generator/render_html.js`, which drives
chrome-headless-shell through Puppeteer with JavaScript off and every non-data URL blocked.

The launcher handed in is the browser family's own (`memory_mode=BROWSER_MEMORY_MODE`, `nproc=BROWSER_NPROC`,
`nofile=BROWSER_NOFILE`; composition builds it): RLIMIT_AS cannot hold Chromium (V8 and PartitionAlloc reserve tens of
GiB of address space at start), so the browser child is bounded by RLIMIT_DATA per process and by the instance memory
as a whole; NPROC counts threads per uid and Chromium starts with ~60-120. S0-f confirms the numbers.

`CHROMIUM_OWN_SANDBOX`: whether Chromium's namespace + seccomp sandbox runs under the uid boundary. Off, accepted
(the S0-f live check passed without it); a code constant, not an env knob (no new deploy substitution).

**No engine or browser text leaves**: a non-ok answer carries a fixed detail code only. No repair loop consumes the text
(unlike render.docx, RFC §4.9) and the browser's stderr can echo attacker-chosen URLs; the client maps statuses to fixed
texts. The child's stderr is reported by the launcher as length + SHA-256 only.

**The workdir is writable by the untrusted child**, so `out.bin` is read the docx engine's way (copied, not imported: an
engine module imports no other module's privates): opened relative to one directory descriptor with O_NOFOLLOW |
O_NONBLOCK, `fstat`-ed, a single-link regular file within the response cap, copied into a fresh `mkdtemp` directory
under the server-only `output_root`. The caller removes `dirname(output_path)` after use. All file IO runs off the loop.

Status map: wall clock or SIGXCPU -> timeout; SIGXFSZ -> too_large; exit 4 -> bad_request; exit 5 -> error
(engine_exec_failed); exit 2 -> error (browser_unavailable); exit 1 -> script_error (render_failed); any other signal ->
error (engine_killed); any other exit -> error (engine_exit); exit 0 with a missing or empty out.bin -> empty_output;
over the response cap -> too_large; else ok. One WARNING line per non-ok answer (op, status, detail, wall).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import Optional, Tuple

from ...domain.sandbox_contract import (
    RESPONSE_CAP_BYTES, SandboxEngineResult, SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus,
)
from ...utils.logger import logger
from .child_launcher import ChildLauncher
from .render_html_child import EXIT_BAD_REQUEST, EXIT_EXEC_FAILED, MODE_PDF, MODE_PNG, OUTPUT_FILE

# The browser family's launcher limits (RFC §4.6 / §11 deltas D2). Chosen here; S0-f decides.
BROWSER_NPROC = 512
BROWSER_NOFILE = 1024
BROWSER_MEMORY_MODE = "data"
# Chromium's own sandbox under the engine uid (S0-f decides; False = `--no-sandbox`, as the in-process runners today).
CHROMIUM_OWN_SANDBOX = False

# render_html.js exit codes.
EXIT_RENDER_FAILED = 1
EXIT_BROWSER_UNAVAILABLE = 2

# Detail codes: the only text a non-ok answer carries.
DETAIL_WALL_CLOCK = "wall_clock"
DETAIL_CPU_LIMIT = "cpu_limit"
DETAIL_FILE_SIZE_LIMIT = "file_size_limit"
DETAIL_RESPONSE_CAP = "response_cap"
DETAIL_BAD_REQUEST = "bad_request"
DETAIL_EXEC_FAILED = "engine_exec_failed"
DETAIL_BROWSER_UNAVAILABLE = "browser_unavailable"
DETAIL_RENDER_FAILED = "render_failed"
DETAIL_KILLED = "engine_killed"
DETAIL_EXIT = "engine_exit"
DETAIL_EMPTY_OUTPUT = "empty_output"
DETAIL_OUTPUT_NOT_REGULAR = "output_not_regular"
DETAIL_WORKDIR = "workdir_unavailable"

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_html_child.py")
_MODES = {SandboxOp.RENDER_PDF: MODE_PDF, SandboxOp.RENDER_PNG: MODE_PNG}
_COPY_CHUNK = 1024 * 1024

_Outcome = Tuple[SandboxStatus, str, Optional[str]]


class HtmlRenderEngine:
    def __init__(self, launcher: ChildLauncher, *, op: SandboxOp, output_root: Optional[str], browser_cache_dir: str,
                 chromium_sandbox: bool = CHROMIUM_OWN_SANDBOX, python: str = sys.executable,
                 child_script: str = _CHILD_SCRIPT, response_cap_bytes: int = RESPONSE_CAP_BYTES) -> None:
        """`output_root` None = a private directory under the server's temp dir. `browser_cache_dir` is where Puppeteer
        finds chrome-headless-shell; it is not checked here (a missing browser answers error/browser_unavailable)."""
        if op not in _MODES:
            raise ValueError(f"not an HTML render op: {op}")
        if not browser_cache_dir:
            raise ValueError("browser_cache_dir is required")
        self._op = op
        self._mode = _MODES[op]
        self._output_root = _checked_output_root(output_root)
        self._launcher = launcher
        self._browser_cache_dir = browser_cache_dir
        self._sandbox_flag = "1" if chromium_sandbox else "0"
        self._python = python
        self._child_script = child_script
        self._response_cap = response_cap_bytes

    async def run(self, body_path: str, workdir: str, limits: SandboxLimits, meta: SandboxMeta) -> SandboxEngineResult:
        """`meta` is the contract's decoded request metadata; these ops need none of it."""
        started = time.monotonic()
        child = await self._launcher.run(
            [self._python, "-B", "-I", self._child_script, body_path, workdir, self._mode, self._browser_cache_dir,
             self._sandbox_flag],
            workdir=workdir, env={}, limits=limits,
            stdin_path=os.devnull, stdout_path=os.devnull, op=self._op.value.replace(".", "_"),
        )
        outcome = _exit_outcome(child.timed_out, child.returncode)
        if outcome is None:
            outcome = await asyncio.to_thread(_collect_output, workdir, self._output_root, self._response_cap)
        status, detail, output_path = outcome
        wall_s = time.monotonic() - started
        if status is not SandboxStatus.OK:
            logger.warning(f"[Sandbox] render op={self._op.value} status={status.value} detail={detail} "
                           f"wall_s={wall_s:.3f}")
        return SandboxEngineResult(status=status, output_path=output_path, detail=detail,
                                   peak_rss_bytes=child.peak_rss_bytes, wall_s=wall_s)


# -- exit mapping -------------------------------------------------------------------------------------------------

def _exit_outcome(timed_out: bool, code: int) -> Optional[_Outcome]:
    """The non-ok answer for a child that did not exit 0, or None when its output is to be collected."""
    if timed_out:
        return SandboxStatus.TIMEOUT, DETAIL_WALL_CLOCK, None
    if code == 0:
        return None
    if code == -signal.SIGXCPU:
        return SandboxStatus.TIMEOUT, DETAIL_CPU_LIMIT, None
    if code == -signal.SIGXFSZ:
        return SandboxStatus.TOO_LARGE, DETAIL_FILE_SIZE_LIMIT, None
    if code < 0:
        return SandboxStatus.ERROR, DETAIL_KILLED, None
    if code == EXIT_BAD_REQUEST:
        return SandboxStatus.BAD_REQUEST, DETAIL_BAD_REQUEST, None
    if code == EXIT_EXEC_FAILED:
        return SandboxStatus.ERROR, DETAIL_EXEC_FAILED, None
    if code == EXIT_BROWSER_UNAVAILABLE:
        return SandboxStatus.ERROR, DETAIL_BROWSER_UNAVAILABLE, None
    if code == EXIT_RENDER_FAILED:
        return SandboxStatus.SCRIPT_ERROR, DETAIL_RENDER_FAILED, None
    return SandboxStatus.ERROR, DETAIL_EXIT, None


# -- files (all blocking: callers run them off the event loop) -----------------------------------------------------

class NotRegularFile(OSError):
    """A file in the engine-writable workdir is not a single-link regular file."""


def _checked_output_root(output_root: Optional[str]) -> str:
    if output_root is None:
        return tempfile.mkdtemp(prefix="sandbox_out_")   # 0700, owned by this process
    info = os.stat(output_root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        logger.error("[Sandbox] output root is not a directory owned by this process and closed to group/other")
        raise ValueError("output_root must be a directory owned by this process, not group/world-writable")
    return output_root


def _open_regular(name: str, dir_fd: int) -> Tuple[int, int]:
    """(fd, size) of a single-link regular file in the workdir, opened without following links or blocking."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NotRegularFile(name)
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


def _create_private(path: str) -> int:
    """A new file only: an existing entry (or link) at `path` is an error."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _collect_output(workdir: str, output_root: str, response_cap: int) -> _Outcome:
    try:
        dir_fd = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        logger.warning(f"[Sandbox] render workdir refused: {type(exc).__name__}")
        return SandboxStatus.ERROR, DETAIL_WORKDIR, None
    try:
        try:
            fd, size = _open_regular(OUTPUT_FILE, dir_fd)
        except FileNotFoundError:
            return SandboxStatus.EMPTY_OUTPUT, DETAIL_EMPTY_OUTPUT, None
        except OSError as exc:
            logger.warning(f"[Sandbox] render out.bin refused: {type(exc).__name__}")
            return SandboxStatus.ERROR, DETAIL_OUTPUT_NOT_REGULAR, None
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as source:
        if size == 0:
            return SandboxStatus.EMPTY_OUTPUT, DETAIL_EMPTY_OUTPUT, None
        if size > response_cap:
            logger.warning(f"[Sandbox] render output {size} bytes exceeds the cap {response_cap}")
            return SandboxStatus.TOO_LARGE, DETAIL_RESPONSE_CAP, None
        out_dir = tempfile.mkdtemp(prefix="render_out_", dir=output_root)
        target = os.path.join(out_dir, OUTPUT_FILE)
        try:
            copied = 0
            with os.fdopen(_create_private(target), "wb") as sink:
                while chunk := source.read(min(_COPY_CHUNK, response_cap + 1 - copied)):
                    sink.write(chunk)
                    copied += len(chunk)
        except BaseException:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
    if copied == 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        return SandboxStatus.EMPTY_OUTPUT, DETAIL_EMPTY_OUTPUT, None
    if copied > response_cap:
        shutil.rmtree(out_dir, ignore_errors=True)
        return SandboxStatus.TOO_LARGE, DETAIL_RESPONSE_CAP, None
    return SandboxStatus.OK, "", target
