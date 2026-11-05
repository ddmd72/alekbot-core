"""
ParseEngine: the service's `parse.*` engine (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6-§4.9; plan P1). One instance per op:

    parse.extract          one child, `{"op": "extract"}`, on `source.<ext>` (a hard link of the request body)
    parse.pdf_page_count   one child, `{"op": "pdf_page_count"}`, on the body
    parse.pdf_windows      one child per window (`{"op": "cut_pdf_window"}`), in order, under one wall clock
    parse.container_facts  one child, `{"op": "container_facts"}`, on the body (EXTERNAL_FILES D13, owner decision O2):
                           the ZIP facts of `document_hardening.zip_container_facts`; its status (`zip`, `not_zip`,
                           `too_large`, `error`) is the detail of an ok answer like a parser status

The child is the drive parser's own (`drive_parser/child.py`, through `parse_child.py`), unchanged, started by the
`ChildLauncher` (own uid, allowlisted environment, `limits_exec` applied before it loads). Each child re-applies the
same memory, CPU and file limits from its request, never more: the request carries no `nproc`, and a request above the
launcher's limits exits 4.

**The workdir is writable by the untrusted child**, so the engine never trusts a path in it. The request JSON is written
by this process (O_EXCL | O_NOFOLLOW) before the child starts and reaches it as stdin. The child's result and window
files are opened relative to one directory descriptor with O_NOFOLLOW | O_NONBLOCK, `fstat`-ed, and must be single-link
regular files within their cap (the docx engine's discipline, copied: an engine module imports no other module's
privates). What leaves is only ever a copy in a fresh `mkdtemp` directory under the server-only `output_root`, so the
caller never opens anything inside the engine-writable workdir by path.

Status convention (RFC §4.7): `ok` means the child ran and wrote a result that passed validation. The parser's own status
(`text`, `no_text`, `unsupported`, `too_large`, `encrypted`, `error`) is the detail of an `ok` answer and travels in the
body (`decode_parse_result` / `encode_parse_result`). A run failure is a non-ok status: `timeout`, `memory`, `error`,
`bad_request`, `too_large`. `pdf_windows` is all or nothing: a window whose result is not `text` answers `ok`, detail =
that status and a `{status, detail}` body with no frame; any run failure removes the output and returns that failure.
One WARNING line per failure (op, status, detail, wall); never the kind, the extension, the window plan or child output.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ...domain.sandbox_contract import (
    RESPONSE_CAP_BYTES, SandboxEngineResult, SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus,
)
from ...domain.sandbox_parse_result import PARSE_RESULT_MAX_BYTES, ParseResultError, decode_parse_result, encode_parse_result
from ...domain.sandbox_windows_frame import frame_prefix, window_prefix
from ...utils.logger import logger
from .child_launcher import ChildLauncher, ChildResult

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parse_child.py")
OUTPUT_FILE = "out.bin"
_COPY_CHUNK = 1024 * 1024

# The drive child's exit codes (drive_parser/child.py). Mirrored, not imported (REQ-ARCH-23); a test pins them in step.
EXIT_MEMORY = 3
EXIT_BAD_REQUEST = 4
# `subprocess_content_extractor._FIXED_ENV`, mirrored (a top-level adapter is not imported); a test pins it. One thread
# per numeric library: each pool reserves address space that RLIMIT_AS counts.
CHILD_ENV: Mapping[str, str] = {
    "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2",
}

_NO_RESULT = "no valid result"
_PARSE_OPS = (SandboxOp.PARSE_EXTRACT, SandboxOp.PARSE_PDF_PAGE_COUNT, SandboxOp.PARSE_PDF_WINDOWS,
              SandboxOp.PARSE_CONTAINER_FACTS)


class NotRegularFile(OSError):
    """A file in the engine-writable workdir is not a single-link regular file."""


class _Failure(Exception):
    """A run that ends with a non-ok status (never escapes `run`)."""

    def __init__(self, status: SandboxStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ParseEngine:
    def __init__(self, launcher: ChildLauncher, *, op: SandboxOp, output_root: Optional[str] = None,
                 python: str = sys.executable, child_script: str = _CHILD_SCRIPT,
                 response_cap_bytes: int = RESPONSE_CAP_BYTES) -> None:
        if op not in _PARSE_OPS:
            raise ValueError(f"not a parse op: {op}")
        self._op = op
        self._output_root = _checked_output_root(output_root)
        self._launcher = launcher
        self._python = python
        self._child_script = child_script
        self._response_cap = response_cap_bytes

    async def run(self, body_path: str, workdir: str, limits: SandboxLimits, meta: SandboxMeta) -> SandboxEngineResult:
        started = time.monotonic()
        peak: List[int] = [0]
        try:
            output_path, detail = await self._run(body_path, workdir, limits, meta, started, peak)
            status = SandboxStatus.OK
        except _Failure as failure:
            output_path, status, detail = None, failure.status, failure.detail
        wall_s = time.monotonic() - started
        if status is not SandboxStatus.OK:
            logger.warning(f"[Sandbox] parse op={self._op.value} status={status.value} detail={detail} wall_s={wall_s:.3f}")
        return SandboxEngineResult(status=status, output_path=output_path, detail=detail,
                                   peak_rss_bytes=peak[0], wall_s=wall_s)

    # -- ops ------------------------------------------------------------------------------------------------------

    async def _run(self, body_path: str, workdir: str, limits: SandboxLimits, meta: SandboxMeta,
                   started: float, peak: List[int]) -> Tuple[str, str]:
        """(output path, ok detail); raises _Failure."""
        if self._op is SandboxOp.PARSE_EXTRACT:
            if not meta.kind:
                raise _Failure(SandboxStatus.BAD_REQUEST, "kind is required")
            source = await asyncio.to_thread(_link_source, body_path, workdir, meta.ext)
            result = await self._child(0, {"op": "extract", "path": source, "kind": meta.kind},
                                       workdir, limits, started, peak)
            return await self._answer(result)
        if self._op is SandboxOp.PARSE_PDF_PAGE_COUNT:
            result = await self._child(0, {"op": "pdf_page_count", "path": body_path}, workdir, limits, started, peak)
            return await self._answer(result)
        if self._op is SandboxOp.PARSE_CONTAINER_FACTS:
            result = await self._child(0, {"op": "container_facts", "path": body_path}, workdir, limits, started, peak)
            return await self._answer(result)
        if not meta.windows:
            raise _Failure(SandboxStatus.BAD_REQUEST, "windows are required")
        return await self._windows(body_path, workdir, limits, meta.windows, started, peak)

    async def _windows(self, body_path: str, workdir: str, limits: SandboxLimits,
                       plan: Tuple[Tuple[int, int], ...], started: float, peak: List[int]) -> Tuple[str, str]:
        names: List[str] = []
        total = len(frame_prefix(len(plan)))
        for index, (first_page, page_count) in enumerate(plan):
            name = f"window-{index}.pdf"
            request = {"op": "cut_pdf_window", "path": body_path, "first_page": first_page,
                       "page_count": page_count, "out_path": os.path.join(workdir, name)}
            result = await self._child(index, request, workdir, limits, started, peak)
            if result["status"] != "text":   # a parser outcome for the whole batch: ok + detail, no frame
                return await self._answer({k: v for k, v in result.items() if k in ("status", "detail")})
            size = await asyncio.to_thread(_window_size, workdir, name)
            total += len(window_prefix(size)) + size
            if total > self._response_cap:
                raise _Failure(SandboxStatus.TOO_LARGE, "output exceeded the response cap")
            names.append(name)
        path = await asyncio.to_thread(_write_frame, workdir, names, self._output_root, self._response_cap)
        return path, "text"

    async def _answer(self, result: Dict[str, Any]) -> Tuple[str, str]:
        try:
            body = encode_parse_result(result)
        except ParseResultError:
            raise _Failure(SandboxStatus.ERROR, _NO_RESULT) from None
        if len(body) > self._response_cap:
            raise _Failure(SandboxStatus.TOO_LARGE, "output exceeded the response cap")
        try:
            path = await asyncio.to_thread(_write_output, body, self._output_root)
        except OSError as exc:
            logger.warning(f"[Sandbox] parse output could not be written: {type(exc).__name__}")
            raise _Failure(SandboxStatus.ERROR, "output could not be written") from None
        return path, str(result["status"])

    # -- one child ------------------------------------------------------------------------------------------------

    async def _child(self, index: int, fields: Mapping[str, Any], workdir: str, limits: SandboxLimits,
                     started: float, peak: List[int]) -> Dict[str, Any]:
        """Runs one child and returns its validated result. Raises _Failure for every way it can fail."""
        remaining = limits.wall_s - (time.monotonic() - started)
        if remaining <= 0:
            raise _Failure(SandboxStatus.TIMEOUT, f"wall clock {limits.wall_s:g}s")
        result_name = f"result-{index}.json"
        request = {**fields, "out": os.path.join(workdir, result_name), "memory_bytes": limits.memory_bytes,
                   "cpu_s": math.ceil(limits.cpu_s), "file_bytes": limits.file_bytes}
        request_path = os.path.join(workdir, f"request-{index}.json")
        try:
            await asyncio.to_thread(_write_request, request_path, request)
        except OSError as exc:
            logger.warning(f"[Sandbox] parse request file refused: {type(exc).__name__}")
            raise _Failure(SandboxStatus.ERROR, "request could not be staged") from None
        child = await self._launcher.run(
            [self._python, "-B", "-I", self._child_script], workdir=workdir, env=CHILD_ENV,
            limits=dataclasses.replace(limits, wall_s=remaining),
            stdin_path=request_path, stdout_path=os.devnull, op=self._op.value.replace(".", "_"),
        )
        peak[0] = max(peak[0], child.peak_rss_bytes)
        _raise_for_exit(child, limits, remaining)
        raw = await asyncio.to_thread(_read_result, workdir, result_name)
        try:
            if raw is None:
                raise ParseResultError(_NO_RESULT)
            return decode_parse_result(raw, self._op)
        except ParseResultError:
            raise _Failure(SandboxStatus.ERROR, _NO_RESULT) from None


# -- exit mapping -------------------------------------------------------------------------------------------------

def _raise_for_exit(child: ChildResult, limits: SandboxLimits, remaining: float) -> None:
    """The in-process adapter's map (`SubprocessContentExtractor._exit_status`)."""
    code = child.returncode
    if child.timed_out:
        raise _Failure(SandboxStatus.TIMEOUT, f"wall clock {remaining:g}s")
    if code == 0:
        return
    if code == -signal.SIGXCPU:
        raise _Failure(SandboxStatus.TIMEOUT, f"cpu limit {math.ceil(limits.cpu_s)}s")
    if code == EXIT_MEMORY or code == -signal.SIGKILL:
        raise _Failure(SandboxStatus.MEMORY, "MemoryError" if code == EXIT_MEMORY else "killed")
    if code == EXIT_BAD_REQUEST:
        raise _Failure(SandboxStatus.ERROR, "child refused the request")
    raise _Failure(SandboxStatus.ERROR, f"exit {code}")


# -- files (all blocking: callers run them off the event loop) -----------------------------------------------------

def _checked_output_root(output_root: Optional[str]) -> str:
    if output_root is None:
        return tempfile.mkdtemp(prefix="sandbox_out_")   # 0700, owned by this process
    info = os.stat(output_root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        logger.error("[Sandbox] output root is not a directory owned by this process and closed to group/other")
        raise ValueError("output_root must be a directory owned by this process, not group/world-writable")
    return output_root


def _link_source(body_path: str, workdir: str, ext: Optional[str]) -> str:
    """`source.<ext>` (or `source`): a hard link of the body, made before the child runs."""
    source = os.path.join(workdir, f"source.{ext}" if ext else "source")
    try:
        os.link(body_path, source)
    except OSError as exc:
        logger.warning(f"[Sandbox] parse source could not be linked: {type(exc).__name__}")
        raise _Failure(SandboxStatus.ERROR, "source could not be staged") from None
    return source


def _write_request(path: str, request: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps(request).encode("utf-8"))


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


def _open_workdir(workdir: str) -> int:
    return os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _read_result(workdir: str, name: str) -> Optional[bytes]:
    """The child's result file, or None when it is missing, not a plain file or over the bound."""
    try:
        dir_fd = _open_workdir(workdir)
    except OSError as exc:
        logger.warning(f"[Sandbox] parse workdir refused: {type(exc).__name__}")
        return None
    try:
        fd, size = _open_regular(name, dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as handle:
        if size > PARSE_RESULT_MAX_BYTES:
            return None
        return handle.read(PARSE_RESULT_MAX_BYTES + 1)


def _window_size(workdir: str, name: str) -> int:
    """Size of a window file the child wrote; anything but a non-empty single-link regular file fails the run."""
    try:
        dir_fd = _open_workdir(workdir)
        try:
            fd, size = _open_regular(name, dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        logger.warning(f"[Sandbox] parse window file refused: {type(exc).__name__}")
        raise _Failure(SandboxStatus.ERROR, _NO_RESULT) from None
    os.close(fd)
    if size == 0:
        raise _Failure(SandboxStatus.ERROR, _NO_RESULT)
    return size


def _create_private(path: str) -> int:
    """A new file only: an existing entry (or link) at `path` is an error."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _write_output(body: bytes, output_root: str) -> str:
    out_dir = tempfile.mkdtemp(prefix="parse_out_", dir=output_root)
    try:
        with os.fdopen(_create_private(os.path.join(out_dir, OUTPUT_FILE)), "wb") as sink:
            sink.write(body)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    return os.path.join(out_dir, OUTPUT_FILE)


def _write_frame(workdir: str, names: List[str], output_root: str, cap: int) -> str:
    """The window frame, streamed from the validated window files into a fresh output directory. A window that changed
    size since it was checked, or a frame over the cap, fails the run and leaves no output directory."""
    out_dir = tempfile.mkdtemp(prefix="parse_out_", dir=output_root)
    target = os.path.join(out_dir, OUTPUT_FILE)
    try:
        dir_fd = _open_workdir(workdir)
        try:
            with os.fdopen(_create_private(target), "wb") as sink:
                sink.write(frame_prefix(len(names)))
                written = len(frame_prefix(len(names)))
                for name in names:
                    fd, size = _open_regular(name, dir_fd)
                    with os.fdopen(fd, "rb") as source:
                        written += len(window_prefix(size)) + size
                        if size == 0 or written > cap:
                            raise _Failure(SandboxStatus.TOO_LARGE if size else SandboxStatus.ERROR,
                                           "output exceeded the response cap" if size else _NO_RESULT)
                        sink.write(window_prefix(size))
                        copied = 0
                        while chunk := source.read(min(_COPY_CHUNK, size + 1 - copied)):
                            sink.write(chunk)
                            copied += len(chunk)
                        if copied != size:
                            raise _Failure(SandboxStatus.ERROR, _NO_RESULT)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        logger.warning(f"[Sandbox] parse window file refused: {type(exc).__name__}")
        raise _Failure(SandboxStatus.ERROR, _NO_RESULT) from None
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    return target
