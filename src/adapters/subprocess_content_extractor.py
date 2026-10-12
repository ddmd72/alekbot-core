"""
SubprocessContentExtractor — the drive parser (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.3).

Each file is read by a fresh child Python process (`src/adapters/drive_parser/child.py`) with RLIMIT_AS,
RLIMIT_CPU and RLIMIT_FSIZE applied before any parser loads, a wall-clock kill of its whole process group, an
allowlisted environment (no credentials) and a private temp directory removed afterwards. Parsers amplify
(Phase 0: a 5 KB XLSX peaked at ~1.75 GB), so only a killed process bounds them. A kill is an outcome: `timeout`
(wall clock, SIGXCPU) or `memory` (MemoryError, SIGKILL from the kernel); the indexer process never parses a byte.

The memory, CPU and file limits below are placeholders until spike S1 (§18) measures the Phase 0 bomb set on the
indexer service (`scripts/infrastructure/drive_extract_s1.py`).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from src.domain.drive_extraction import (
    PARSER, ExtractionOutcome, ExtractionRequest, ExtractionStatus, PdfSplit, PdfSplitFailed,
)
from src.domain.drive_index_params import EXTRACT_WALL_TIMEOUT_S, MAX_TEXT_CHARS
from src.domain.drive_index_policy import size_cap
from src.ports.content_extractor_port import ContentExtractorPort
from src.utils.logger import logger

EXTRACT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024    # RLIMIT_AS of the child (S1 placeholder; indexer has 2 GiB)
EXTRACT_CPU_LIMIT_S = 90                           # RLIMIT_CPU soft limit (S1 placeholder; below the wall clock)
EXTRACT_FILE_LIMIT_BYTES = 64 * 1024 * 1024        # RLIMIT_FSIZE: the result, a PDF window, the stderr file
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
_STDERR_TAIL = 400


class SubprocessContentExtractor(ContentExtractorPort):
    """The `parser` link of every extractor chain; also cuts routed PDFs into page windows for the reader."""

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

    async def split_pdf(self, path: str, pages_per_window: int, max_pages: int, out_dir: str) -> PdfSplit:
        """Window PDFs in `out_dir`, cut by the sandboxed child (§24.3); the caller owns `out_dir` and its files.
        Raises PdfSplitFailed with the outcome status when the PDF cannot be cut."""
        result, status, detail = await self._run({"op": "split_pdf", "path": path,
                                                  "pages_per_window": pages_per_window, "max_pages": max_pages,
                                                  "out_dir": out_dir})
        if status is not None or result is None:
            raise PdfSplitFailed(status or ExtractionStatus.ERROR, detail)
        return self._split_from_result(result, out_dir)

    # -- child process ----------------------------------------------------------------------------------------------

    async def _run(self, payload: Mapping[str, Any]
                   ) -> Tuple[Optional[Dict[str, Any]], Optional[ExtractionStatus], str]:
        """(result, None, "") when the child wrote a result; (None, status, detail) when it was killed or failed."""
        async with self._slots:
            workdir = tempfile.mkdtemp(prefix="drive-extract-", dir=self._temp_root)
            started = time.monotonic()
            try:
                out = os.path.join(workdir, "result.json")
                errors = os.path.join(workdir, "stderr.txt")
                request = {**payload, "out": out, "memory_bytes": self._memory, "cpu_s": self._cpu,
                           "file_bytes": self._file_bytes}
                returncode, timed_out = await self._spawn(request, workdir, errors)
                status, detail = self._exit_status(returncode, timed_out)
                elapsed = time.monotonic() - started
                if status is not None:
                    if status is ExtractionStatus.ERROR:
                        detail = f"{detail}; {_tail(errors)}"
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

    async def _spawn(self, request: Mapping[str, Any], workdir: str, errors: str) -> Tuple[int, bool]:
        with open(errors, "wb") as stderr:
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

    @staticmethod
    def _split_from_result(result: Dict[str, Any], out_dir: str) -> PdfSplit:
        status = result.get("status")
        if status != ExtractionStatus.TEXT.value:
            try:
                failed = ExtractionStatus(status)
            except ValueError:
                failed = ExtractionStatus.ERROR
            raise PdfSplitFailed(failed, str(result.get("detail", ""))[:300])
        paths = result.get("paths")
        pages_total = result.get("pages_total")
        root = os.path.realpath(out_dir)
        if (not isinstance(paths, list) or not isinstance(pages_total, int)
                or not all(isinstance(p, str) and os.path.dirname(os.path.realpath(p)) == root
                           and os.path.isfile(p) for p in paths)):
            logger.warning("[DriveExtract] split result names files outside its window directory")
            raise PdfSplitFailed(ExtractionStatus.ERROR, "invalid split result")
        return PdfSplit(paths=tuple(paths), pages_total=pages_total, truncated=bool(result.get("truncated")))


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


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - _STDERR_TAIL))
            return fh.read().decode("utf-8", errors="replace").strip().replace("\n", " | ")
    except OSError:
        return ""
