"""
SubprocessContentExtractor — the drive parser (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.3).

Each file is read by a fresh child Python process (`src/adapters/drive_parser/child.py`) with RLIMIT_AS,
RLIMIT_CPU and RLIMIT_FSIZE applied before any parser loads, a wall-clock kill of its whole process group, an
allowlisted environment and a private temp directory removed afterwards. Parsers amplify (Phase 0: a 5 KB XLSX
peaked at ~1.75 GB), so only a killed process bounds them. A kill is an outcome: `timeout` (wall clock, SIGXCPU) or
`memory` (MemoryError, SIGKILL from the kernel); the indexer process never parses a byte.

This is resource isolation, not code-execution containment. The child runs as the same user, in the same container
and network as the indexer: code an exploited parser runs there can read the parent's environment through
`/proc/<ppid>/environ` (the allowlist keeps credentials out of the child's own environment, not out of its reach),
ask the metadata server for the service account's token, and reach the network. Accepted for a single-owner
deployment (RFC known limits); a parser CVE with code execution, or users who do not trust one another, is the
trigger for a real boundary (a separate sandboxed service or gVisor-style isolation without credentials).

The memory, CPU and file limits below are placeholders until spike S1 (§18) measures the Phase 0 bomb set on the
indexer service (`scripts/infrastructure/drive_extract_s1.py`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import IO, Any, Dict, Mapping, Optional, Tuple

from src.domain.drive_extraction import (
    PARSER, ExtractionOutcome, ExtractionRequest, ExtractionStatus, PdfSplitFailed, text_digest,
)
from src.domain.drive_index_params import EXTRACT_WALL_TIMEOUT_S, MAX_TEXT_CHARS
from src.domain.drive_index_policy import size_cap
from src.ports.content_extractor_port import ContentExtractorPort
from src.utils.logger import logger

EXTRACT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024    # RLIMIT_AS of the child (S1 placeholder; indexer has 2 GiB)
EXTRACT_CPU_LIMIT_S = 90                           # RLIMIT_CPU soft limit (S1 placeholder; below the wall clock)
EXTRACT_FILE_LIMIT_BYTES = 64 * 1024 * 1024        # RLIMIT_FSIZE: the result, one PDF window, the stderr file
CHILD_MODULE = "src.adapters.drive_parser.child"
# The child's exit codes (drive_parser/child.py). Mirrored, not imported: a top-level adapter imports no other
# adapter module (REQ-ARCH-08); a test pins the two in step.
EXIT_MEMORY = 3
EXIT_BAD_REQUEST = 4

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")
# One thread per numeric library: each pool reserves address space that RLIMIT_AS counts.
_FIXED_ENV = {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2"}
_RESULT_MAX_BYTES = MAX_TEXT_CHARS * 6 + 1024 * 1024   # JSON of MAX_TEXT_CHARS characters, escapes included
_CHILD_STATUSES = frozenset({ExtractionStatus.TEXT, ExtractionStatus.NO_TEXT, ExtractionStatus.UNSUPPORTED,
                             ExtractionStatus.TOO_LARGE, ExtractionStatus.ENCRYPTED, ExtractionStatus.ERROR})
_STDERR_TAIL = 400   # bytes of the child's stderr hashed into an `error` detail; the text itself is never logged


class SubprocessContentExtractor(ContentExtractorPort):
    """The `parser` link of every extractor chain; also counts a routed PDF's pages and cuts its page windows for
    the reader, one window per call (§24.3)."""

    VERSION = 1

    def __init__(self, *, memory_limit_bytes: int = EXTRACT_MEMORY_LIMIT_BYTES,
                 cpu_limit_s: int = EXTRACT_CPU_LIMIT_S, wall_timeout_s: float = EXTRACT_WALL_TIMEOUT_S,
                 file_limit_bytes: int = EXTRACT_FILE_LIMIT_BYTES, max_children: int = 1,
                 python: str = sys.executable, child_module: str = CHILD_MODULE,
                 temp_root: Optional[str] = None) -> None:
        self._memory = memory_limit_bytes
        self._cpu = cpu_limit_s
        self._wall = wall_timeout_s
        self._file_bytes = file_limit_bytes
        self._python = python
        self._module = child_module
        self._temp_root = temp_root
        # One child at a time by default: two children at the memory limit would exceed the instance (§9, S1).
        self._slots = asyncio.Semaphore(max_children)

    @property
    def extractor_id(self) -> str:
        return PARSER

    @property
    def version(self) -> int:
        return self.VERSION

    @property
    def stamp(self) -> str:
        return f"{PARSER}@{self.VERSION}"

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        try:
            size = os.path.getsize(request.path)
        except OSError as exc:
            logger.warning(f"[DriveExtract] cannot stat the downloaded file: {type(exc).__name__}")
            return self._outcome(ExtractionStatus.ERROR, detail=f"stat: {type(exc).__name__}")
        if size > size_cap(request.kind):         # the sniffed kind may carry a smaller cap than the declared one
            return self._outcome(ExtractionStatus.TOO_LARGE, detail=f"{size} bytes for kind {request.kind}")
        result, status, detail = await self._run({"op": "extract", "path": request.path, "kind": request.kind})
        if status is not None or result is None:
            return self._outcome(status or ExtractionStatus.ERROR, detail=detail)
        return self._from_result(result)

    async def pdf_page_count(self, path: str) -> int:
        """Pages of the PDF, counted by the sandboxed child (§24.3). Raises PdfSplitFailed with the outcome status
        when the PDF cannot be read."""
        result = await self._pdf_op({"op": "pdf_page_count", "path": path})
        pages = result.get("pages_total")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
            logger.warning("[DriveExtract] page count result has an invalid shape")
            raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid page count")
        return pages

    async def cut_pdf_window(self, path: str, first_page: int, page_count: int, out_path: str) -> None:
        """One window PDF at `out_path` (pages [first_page, first_page + page_count), 0-based, clamped to the end),
        cut by the sandboxed child (§24.3). The caller owns `out_path` and removes it; cutting one window per model
        call keeps at most MODEL_IN_FLIGHT windows on disk. Raises PdfSplitFailed with the outcome status."""
        await self._pdf_op({"op": "cut_pdf_window", "path": path, "first_page": first_page,
                            "page_count": page_count, "out_path": out_path})
        try:
            regular = stat.S_ISREG(os.lstat(out_path).st_mode)   # lstat: a link is not the window
        except OSError:
            regular = False
        if not regular:
            logger.warning("[DriveExtract] cut result is not a regular window file")
            with contextlib.suppress(OSError):
                os.unlink(out_path)
            raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid window")

    # -- child process ----------------------------------------------------------------------------------------------

    async def _run(self, payload: Mapping[str, Any]
                   ) -> Tuple[Optional[Dict[str, Any]], Optional[ExtractionStatus], str]:
        """(result, None, "") when the child wrote a result; (None, status, detail) when it was killed or failed."""
        async with self._slots:
            workdir = tempfile.mkdtemp(prefix="drive-extract-", dir=self._temp_root)
            started = time.monotonic()
            try:
                out = os.path.join(workdir, "result.json")
                request = {**payload, "out": out, "memory_bytes": self._memory, "cpu_s": self._cpu,
                           "file_bytes": self._file_bytes}
                with open(os.path.join(workdir, "stderr.txt"), "w+b") as errors:
                    returncode, timed_out = await self._spawn(request, workdir, errors)
                    status, detail = self._exit_status(returncode, timed_out)
                    if status is ExtractionStatus.ERROR:
                        # its length and a hash only: a parser's traceback can quote the document (R3 F3-5)
                        detail = f"{detail}; {_stderr_digest(errors)}"
                elapsed = time.monotonic() - started
                if status is not None:
                    logger.warning(f"[DriveExtract] {payload.get('op')} {payload.get('kind', 'pdf')}: "
                                   f"{status.value} ({detail}) after {elapsed:.1f}s")
                    return None, status, detail
                result = _load_result(out)
                if result is None:
                    logger.warning(f"[DriveExtract] {payload.get('op')}: no valid result after {elapsed:.1f}s")
                    return None, ExtractionStatus.ERROR, "no valid result"
                return result, None, ""
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _spawn(self, request: Mapping[str, Any], workdir: str, stderr: IO[bytes]) -> Tuple[int, bool]:
        proc = await asyncio.create_subprocess_exec(
            self._python, "-B", "-E", "-s", "-m", self._module,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=stderr,
            env=_child_env(workdir), cwd=_REPO_ROOT, start_new_session=True,
        )
        timed_out = False
        try:
            await asyncio.wait_for(proc.communicate(json.dumps(request).encode("utf-8")), timeout=self._wall)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if proc.returncode is None:              # wall clock, or the tick was cancelled: kill the whole group
                _kill_group(proc.pid)
                await proc.wait()
        return int(proc.returncode if proc.returncode is not None else -signal.SIGKILL), timed_out

    def _exit_status(self, returncode: int, timed_out: bool) -> Tuple[Optional[ExtractionStatus], str]:
        if timed_out:
            return ExtractionStatus.TIMEOUT, f"wall clock {self._wall:g}s"
        if returncode == 0:
            return None, ""
        if returncode == EXIT_MEMORY:
            return ExtractionStatus.MEMORY, "MemoryError"
        if returncode == -signal.SIGXCPU:
            return ExtractionStatus.TIMEOUT, f"cpu limit {self._cpu}s"
        if returncode == -signal.SIGKILL:
            return ExtractionStatus.MEMORY, "killed"   # the kernel's OOM killer; our own kill is `timed_out`
        if returncode == EXIT_BAD_REQUEST:
            return ExtractionStatus.ERROR, "child refused the request"
        return ExtractionStatus.ERROR, f"exit {returncode}"

    # -- results ----------------------------------------------------------------------------------------------------

    def _outcome(self, status: ExtractionStatus, *, detail: str = "") -> ExtractionOutcome:
        return ExtractionOutcome(status=status, extracted_by=self.stamp, detail=detail)

    def _from_result(self, result: Dict[str, Any]) -> ExtractionOutcome:
        """The child is not trusted further than its sandbox: shape, status set and text length are checked."""
        try:
            status = ExtractionStatus(result["status"])
            text = result.get("text", "")
            pages_total = result.get("pages_total", 0)
            truncated = result.get("truncated", False)
            if (status not in _CHILD_STATUSES or not isinstance(text, str) or not isinstance(pages_total, int)
                    or pages_total < 0 or not isinstance(truncated, bool)):
                raise ValueError("bad field")
        except (KeyError, ValueError, TypeError):
            logger.warning("[DriveExtract] child result has an invalid shape")
            return self._outcome(ExtractionStatus.ERROR, detail="invalid result")
        if len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS], True
        if status is ExtractionStatus.TEXT and not text.strip():
            status = ExtractionStatus.NO_TEXT
        return ExtractionOutcome(status=status, text=text if status is ExtractionStatus.TEXT else "",
                                 pages_total=pages_total, truncated=truncated, extracted_by=self.stamp,
                                 detail=str(result.get("detail", ""))[:300])

    async def _pdf_op(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """The child's result of a page-window op, or PdfSplitFailed with the outcome status."""
        result, status, detail = await self._run(payload)
        if status is not None or result is None:
            raise PdfSplitFailed(status or ExtractionStatus.ERROR, detail)
        if result.get("status") != ExtractionStatus.TEXT.value:
            try:
                failed = ExtractionStatus(result.get("status"))
            except ValueError:
                failed = ExtractionStatus.ERROR
            raise PdfSplitFailed(failed, str(result.get("detail", ""))[:300])
        return result


def _child_env(workdir: str) -> Dict[str, str]:
    """Allowlisted: no credentials, no PYTHON* variables; TMPDIR is the call's own directory, removed after."""
    env = {key: value for key in _SAFE_ENV_KEYS if (value := os.environ.get(key))}
    env.update(_FIXED_ENV)
    env["TMPDIR"] = workdir
    return env


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        logger.debug(f"[DriveExtract] child group {pid} already gone: {type(exc).__name__}")


def _load_result(path: str) -> Optional[Dict[str, Any]]:
    try:
        if os.path.getsize(path) > _RESULT_MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stderr_digest(stderr: IO[bytes]) -> str:
    """`stderr <n> bytes #<hash of its tail>`, read through the parent's own descriptor (the child may rename or
    replace the path). Never the text: a parser's traceback can quote the document (R3 F3-5)."""
    try:
        size = os.fstat(stderr.fileno()).st_size
        stderr.seek(max(0, size - _STDERR_TAIL))
        return f"stderr {size} bytes #{text_digest(stderr.read())}" if size else "stderr empty"
    except OSError:
        return "stderr unreadable"
