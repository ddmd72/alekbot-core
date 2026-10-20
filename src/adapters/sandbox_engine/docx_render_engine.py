"""
DocxRenderEngine: the service's `render.docx` engine (RFC §4.6, §4.7, §4.9; plan G1, G2, G20).

Not `NodeDocxRunner` itself: that runner writes into `docx_generator/`, runs node as the caller's uid and applies no
limits. This engine keeps its observable contract (prelude, error texts) but runs `render_docx_child` through the
`ChildLauncher` (own uid, allowlisted environment, `limits_exec` applied once before the child starts and inherited by
node across its exec).

The server never reads node's stdout through a pipe: the child points node's stdout at `out.bin` and stderr at `err.txt`
in the workdir. **The workdir is writable by the untrusted script** (the prelude is not a boundary), so the engine never
trusts a path in it: both files are opened with O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, `fstat`-ed, and must be regular
files (a symlink to a server-readable file, a FIFO, a device or a directory is refused); sizes come from that fstat.
The workdir itself is opened once (O_DIRECTORY | O_NOFOLLOW) and both files are opened relative to that descriptor, and
must have exactly one link (the child creates both fresh). The error text (the model's own script output, §4.9) is read
from the validated descriptor, bounded by ERROR_TEXT_CAP_BYTES. The output is copied from the validated descriptor into a
server-only output root (`output_root`, never on the engine volume; construction refuses a root not owned by this process
or writable by group/other; default: a private directory made under the server's temp dir) by a fresh mkdtemp directory
and an O_EXCL | O_NOFOLLOW file, and THAT path is returned, so the caller never opens anything inside the engine-writable
workdir by path. The caller removes `dirname(output_path)` after use. All file IO runs off the event loop.
The launcher's own stderr summary (length + SHA-256) covers failures of the Python child itself.
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
    ERROR_TEXT_CAP_BYTES, RESPONSE_CAP_BYTES, SandboxEngineResult, SandboxLimits, SandboxStatus,
)
from ...utils.logger import logger
from .child_launcher import ChildLauncher
from .render_docx_child import ERROR_FILE, EXIT_BAD_REQUEST, OUTPUT_FILE

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_docx_child.py")
_TRUNCATION_MARKER = "\n...[truncated]"
_COPY_CHUNK = 1024 * 1024
EMPTY_OUTPUT_TEXT = "Node.js exited 0 but stdout is empty — no DOCX bytes produced"


class DocxRenderEngine:
    def __init__(self, launcher: ChildLauncher, *, python: str = sys.executable,
                 response_cap_bytes: int = RESPONSE_CAP_BYTES, output_root: Optional[str] = None) -> None:
        self._output_root = _checked_output_root(output_root)
        self._launcher = launcher
        self._python = python
        self._response_cap = response_cap_bytes

    async def run(self, body_path: str, workdir: str, limits: SandboxLimits) -> SandboxEngineResult:
        started = time.monotonic()
        child = await self._launcher.run(
            [self._python, "-B", "-I", _CHILD_SCRIPT, body_path, workdir],
            workdir=workdir, env={}, limits=limits,
            stdin_path=os.devnull, stdout_path=os.devnull, op="render_docx",
        )
        wall_s = time.monotonic() - started

        def result(status: SandboxStatus, detail: str = "", output_path: Optional[str] = None) -> SandboxEngineResult:
            return SandboxEngineResult(status=status, output_path=output_path, detail=detail,
                                       peak_rss_bytes=child.peak_rss_bytes, wall_s=wall_s)

        if child.timed_out:
            return result(SandboxStatus.TIMEOUT, f"Node.js process timed out after {limits.wall_s:g}s")
        if child.returncode == -signal.SIGXFSZ:
            return result(SandboxStatus.TOO_LARGE, "output exceeded the file size limit")
        outcome = await asyncio.to_thread(
            _inspect, workdir, child.returncode, self._output_root, self._response_cap)
        if outcome is None:   # the Python child failed before node ran
            if child.returncode == EXIT_BAD_REQUEST:
                return result(SandboxStatus.BAD_REQUEST, "request body or limits could not be used")
            return result(SandboxStatus.ERROR, f"engine child failed (exit code {child.returncode})")
        return result(*outcome)


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


def _inspect(
    workdir: str, returncode: int, output_root: str, response_cap: int,
) -> Optional[Tuple[SandboxStatus, str, Optional[str]]]:
    """None only when the script failed without err.txt (the Python child died before node ran)."""
    try:
        dir_fd = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        logger.warning(f"[Sandbox] render.docx workdir refused: {type(exc).__name__}")
        return SandboxStatus.ERROR, "engine workdir could not be opened", None
    try:
        if returncode != 0:
            return _read_error_text(returncode, dir_fd)
        return _collect_output(dir_fd, output_root, response_cap)
    finally:
        os.close(dir_fd)


def _read_error_text(returncode: int, dir_fd: int) -> Optional[Tuple[SandboxStatus, str, None]]:
    try:
        fd, _ = _open_regular(ERROR_FILE, dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(f"[Sandbox] render.docx err.txt refused: {type(exc).__name__}")
        return SandboxStatus.ERROR, "engine error file was not a plain file", None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(ERROR_TEXT_CAP_BYTES + 1)
    return SandboxStatus.SCRIPT_ERROR, _error_text(returncode, raw), None


def _create_private(path: str) -> int:
    """A new file only: an existing entry (or link) at `path` is an error."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _collect_output(
    dir_fd: int, output_root: str, response_cap: int,
) -> Tuple[SandboxStatus, str, Optional[str]]:
    try:
        fd, size = _open_regular(OUTPUT_FILE, dir_fd)
    except FileNotFoundError:
        return SandboxStatus.EMPTY_OUTPUT, EMPTY_OUTPUT_TEXT, None
    except OSError as exc:
        logger.warning(f"[Sandbox] render.docx out.bin refused: {type(exc).__name__}")
        return SandboxStatus.ERROR, "engine output was not a plain file", None
    with os.fdopen(fd, "rb") as source:
        if size == 0:
            return SandboxStatus.EMPTY_OUTPUT, EMPTY_OUTPUT_TEXT, None
        if size > response_cap:
            logger.warning(f"[Sandbox] render.docx output {size} bytes exceeds the cap {response_cap}")
            return SandboxStatus.TOO_LARGE, "output exceeded the response cap", None
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
        return SandboxStatus.EMPTY_OUTPUT, EMPTY_OUTPUT_TEXT, None
    if copied > response_cap:
        shutil.rmtree(out_dir, ignore_errors=True)
        return SandboxStatus.TOO_LARGE, "output exceeded the response cap", None
    return SandboxStatus.OK, "", target


def _error_text(returncode: int, raw: bytes) -> str:
    """`exit code N\\n<stderr or "(no stderr)">`, the in-process format, bounded by ERROR_TEXT_CAP_BYTES."""
    text = f"exit code {returncode}\n{raw.decode('utf-8', errors='replace').strip() or '(no stderr)'}"
    encoded = text.encode("utf-8")
    if len(encoded) <= ERROR_TEXT_CAP_BYTES:
        return text
    keep = ERROR_TEXT_CAP_BYTES - len(_TRUNCATION_MARKER.encode())
    return encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
