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
The error text (the model's own script output, §4.9) is read from the validated descriptor, bounded by
ERROR_TEXT_CAP_BYTES. The output is copied from the validated descriptor into a server-owned directory (mode 0700,
created next to the workdir by this process) and THAT path is returned, so the caller never opens anything inside the
engine-writable workdir by path. The caller removes `dirname(output_path)` after use. All file IO runs off the event loop.
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
                 response_cap_bytes: int = RESPONSE_CAP_BYTES) -> None:
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
        if child.returncode != 0:
            outcome = await asyncio.to_thread(_read_error_text, child.returncode, os.path.join(workdir, ERROR_FILE))
            if outcome is None:   # the Python child failed before node ran
                if child.returncode == EXIT_BAD_REQUEST:
                    return result(SandboxStatus.BAD_REQUEST, "request body or limits could not be used")
                return result(SandboxStatus.ERROR, f"engine child failed (exit code {child.returncode})")
            return result(*outcome)
        status, detail, path = await asyncio.to_thread(
            _collect_output, os.path.join(workdir, OUTPUT_FILE), os.path.dirname(os.path.abspath(workdir)),
            self._response_cap)
        return result(status, detail, path)


def _open_regular(path: str) -> Tuple[int, int]:
    """(fd, size) of a regular file opened without following links or blocking; OSError/NotRegular otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NotRegularFile(path)
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


class NotRegularFile(OSError):
    """A file in the engine-writable workdir is not a regular file."""


def _read_error_text(returncode: int, error_path: str) -> Optional[Tuple[SandboxStatus, str, None]]:
    """None when err.txt does not exist (node never ran); an ERROR outcome when it is not a plain file."""
    try:
        fd, _ = _open_regular(error_path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(f"[Sandbox] render.docx err.txt refused: {type(exc).__name__}")
        return SandboxStatus.ERROR, "engine error file was not a plain file", None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(ERROR_TEXT_CAP_BYTES + 1)
    return SandboxStatus.SCRIPT_ERROR, _error_text(returncode, raw), None


def _collect_output(
    output_path: str, parent_dir: str, response_cap: int,
) -> Tuple[SandboxStatus, str, Optional[str]]:
    try:
        fd, size = _open_regular(output_path)
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
        out_dir = tempfile.mkdtemp(prefix="render_out_", dir=parent_dir)   # mode 0700, owned by this process
        target = os.path.join(out_dir, OUTPUT_FILE)
        try:
            copied = 0
            with open(target, "wb") as sink:
                while chunk := source.read(min(_COPY_CHUNK, response_cap + 1 - copied)):
                    sink.write(chunk)
                    copied += len(chunk)
            if copied > response_cap:
                shutil.rmtree(out_dir, ignore_errors=True)
                return SandboxStatus.TOO_LARGE, "output exceeded the response cap", None
        except BaseException:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
    return SandboxStatus.OK, "", target


def _error_text(returncode: int, raw: bytes) -> str:
    """`exit code N\\n<stderr or "(no stderr)">`, the in-process format, bounded by ERROR_TEXT_CAP_BYTES."""
    text = f"exit code {returncode}\n{raw.decode('utf-8', errors='replace').strip() or '(no stderr)'}"
    encoded = text.encode("utf-8")
    if len(encoded) <= ERROR_TEXT_CAP_BYTES:
        return text
    keep = ERROR_TEXT_CAP_BYTES - len(_TRUNCATION_MARKER.encode())
    return encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
