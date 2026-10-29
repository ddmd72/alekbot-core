"""
SubprocessContentExtractor — the drive parser (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9, §24.3).

Each file is read by a fresh child Python process (`src/adapters/drive_parser/child.py`) with RLIMIT_AS,
RLIMIT_CPU and RLIMIT_FSIZE applied before any parser loads, a wall-clock kill of its whole process group, an
allowlisted environment and a private temp directory removed afterwards. Parsers amplify (Phase 0: a 5 KB XLSX
peaked at ~1.75 GB), so only a killed process bounds them. A kill is an outcome: `timeout` (wall clock, SIGXCPU) or
`memory` (MemoryError, SIGKILL from the kernel); the indexer process never parses a byte.

The deployment is multi-user (one whitelist, many users' files on one indexer), so the child also runs as an
unprivileged user (R3 F3-1): `sandbox_user`, which the indexer passes from DRIVE_PARSER_USER — the Dockerfile creates
that user and sets the variable, so the deployed indexer always sandboxes, and a configured user that is missing, is
root, or cannot be switched to fails the start. The child gets that uid/gid and no supplementary groups, a per-run
directory it owns (its TMPDIR and HOME), a read-only link of the source file in it, and writes its window there for
the parent to collect. A child of another uid cannot read the parent's `/proc/<ppid>/environ` (the API keys, the
Microsoft client secret), nor rewrite the app source or site-packages (root-owned, not world-writable: the image
build checks it), nor reach another file's temp directory (root-only). Without a configured user (local development)
the child runs as this process's own user, and the constructor says so in a WARNING.

Still reachable from the child (residual, owner-accepted risk; RFC known limits): the metadata server, and with it the
service account's token, and the network. Closing that needs a separate low-privilege parser service (its own
service account) or gVisor-style isolation without credentials.

The memory, CPU and file limits below are placeholders until spike S1 (§18) measures the Phase 0 bomb set on the
indexer service (`scripts/infrastructure/drive_extract_s1.py`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import errno
import os
import pwd
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import IO, Any, Dict, Mapping, Optional, Tuple

from src.domain.drive_capture import signals_from_json
from src.domain.drive_extraction import (
    PARSER, ExtractionOutcome, ExtractionRequest, ExtractionStatus, PdfSplitFailed, text_digest,
)
from src.domain.drive_index_params import EXTRACT_WALL_TIMEOUT_S, MAX_TEXT_CHARS
from src.domain.drive_index_policy import size_cap
from src.ports.content_extractor_port import ContentExtractorPort
from src.domain.memory_telemetry import SCOPE_SUBPROCESS
from src.utils.logger import logger
from src.utils.memory_scope import memory_scope

EXTRACT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024    # RLIMIT_AS of the child (S1 placeholder; indexer has 2 GiB)
EXTRACT_CPU_LIMIT_S = 90                           # RLIMIT_CPU soft limit (S1 placeholder; below the wall clock)
EXTRACT_FILE_LIMIT_BYTES = 64 * 1024 * 1024        # RLIMIT_FSIZE: the result, one PDF window, the stderr file
# RLIMIT_NPROC of the sandboxed child (per uid: processes and threads). The document child spawns no process and its
# numeric libraries are pinned to one thread; the media child's 256 is the same fork-bomb bound (mirrored, REQ-ARCH-08).
EXTRACT_NPROC_LIMIT = 256
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
# The environment variable naming the unprivileged user the child runs as (R3 F3-1). The Dockerfile sets it and
# creates the user; indexer_main passes it as `sandbox_user`. Unset (local development): no sandbox user.
PARSER_USER_ENV = "DRIVE_PARSER_USER"
_STDERR_TAIL = 400   # bytes of the child's stderr hashed into an `error` detail; the text itself is never logged


class SubprocessContentExtractor(ContentExtractorPort):
    """The `parser` link of every extractor chain; also counts a routed PDF's pages and cuts its page windows for
    the reader, one window per call (§24.3)."""

    # 2: parser@2's HTML policy (§25.3, D23); 3: a PDF's capture signals (§26.3, D34). A bump re-extracts every
    # parser-decided record (§25.4.8).
    VERSION = 3

    def __init__(self, *, memory_limit_bytes: int = EXTRACT_MEMORY_LIMIT_BYTES,
                 cpu_limit_s: int = EXTRACT_CPU_LIMIT_S, wall_timeout_s: float = EXTRACT_WALL_TIMEOUT_S,
                 file_limit_bytes: int = EXTRACT_FILE_LIMIT_BYTES, max_children: int = 1,
                 python: str = sys.executable, child_module: str = CHILD_MODULE,
                 temp_root: Optional[str] = None, sandbox_user: Optional[str] = None) -> None:
        # (uid, gid) the child runs as, or None: this process's own user (local development, logged as a WARNING)
        self._sandbox = _resolve_sandbox(sandbox_user)
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
                staged, window = self._stage(payload, workdir)
                request = {**staged, "out": out, "memory_bytes": self._memory, "cpu_s": self._cpu,
                           "file_bytes": self._file_bytes}
                if self._sandbox is not None:     # root ignores it and a dev user's own processes would count against it
                    request["nproc"] = EXTRACT_NPROC_LIMIT
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
                if window is not None:
                    _collect_window(*window)
                return result, None, ""
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    def _stage(self, payload: Mapping[str, Any], workdir: str
               ) -> Tuple[Dict[str, Any], Optional[Tuple[str, str]]]:
        """The request the child gets, and (where the child writes the window, where the caller wants it) for a cut.
        Unsandboxed: the payload as given. Sandboxed (R3 F3-1): the child cannot enter the caller's root-only temp
        directories, so the run directory becomes the child's, the source is linked into it read-only for the child's
        group (copied when a link is impossible), and a window is written there and collected after the run."""
        if self._sandbox is None:
            return dict(payload), None
        uid, gid = self._sandbox
        os.chown(workdir, uid, gid)                                   # mode 0700 from mkdtemp: the child's alone
        staged: Dict[str, Any] = dict(payload)
        source = str(payload["path"])
        linked = os.path.join(workdir, "source" + os.path.splitext(source)[1][:16])
        try:
            os.link(source, linked)
        except OSError:                                               # another filesystem: a copy
            shutil.copyfile(source, linked)
        os.chown(linked, -1, gid)
        os.chmod(linked, 0o440)                                       # readable, never writable, by the child
        staged["path"] = linked
        window = None
        if "out_path" in payload:
            staged["out_path"] = os.path.join(workdir, "window.pdf")
            window = (staged["out_path"], str(payload["out_path"]))
        return staged, window

    async def _spawn(self, request: Mapping[str, Any], workdir: str, stderr: IO[bytes]) -> Tuple[int, bool]:
        with memory_scope(SCOPE_SUBPROCESS, "drive_parser"):
            return await self._spawn_child(request, workdir, stderr)

    async def _spawn_child(self, request: Mapping[str, Any], workdir: str, stderr: IO[bytes]) -> Tuple[int, bool]:
        identity: Dict[str, Any] = {}
        if self._sandbox is not None:   # no supplementary groups: root's would otherwise be inherited
            identity = {"user": self._sandbox[0], "group": self._sandbox[1], "extra_groups": []}
        proc = await asyncio.create_subprocess_exec(
            self._python, "-B", "-E", "-s", "-m", self._module,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=stderr,
            env=_child_env(workdir, sandboxed=self._sandbox is not None), cwd=_REPO_ROOT, start_new_session=True,
            **identity,
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
            script_rendered = result.get("script_rendered", False)
            if (status not in _CHILD_STATUSES or not isinstance(text, str) or not isinstance(pages_total, int)
                    or pages_total < 0 or not isinstance(truncated, bool) or not isinstance(script_rendered, bool)):
                raise ValueError("bad field")
        except (KeyError, ValueError, TypeError):
            logger.warning("[DriveExtract] child result has an invalid shape")
            return self._outcome(ExtractionStatus.ERROR, detail="invalid result")
        if len(text) > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS], True
        if status is ExtractionStatus.TEXT and not text.strip():
            status = ExtractionStatus.NO_TEXT
        is_text = status is ExtractionStatus.TEXT
        signals = signals_from_json(result["capture_signals"]) if "capture_signals" in result else None
        if "capture_signals" in result and signals is None:
            logger.warning("[DriveExtract] child capture signals have an invalid shape; dropped")
        return ExtractionOutcome(status=status, text=text if is_text else "",
                                 pages_total=pages_total, truncated=truncated, extracted_by=self.stamp,
                                 detail=str(result.get("detail", ""))[:300],
                                 script_rendered=is_text and script_rendered, capture_signals=signals)

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


def _child_env(workdir: str, *, sandboxed: bool = False) -> Dict[str, str]:
    """Allowlisted: no credentials, no PYTHON* variables; TMPDIR is the call's own directory, removed after. A
    sandboxed child's HOME is that directory too: the parent's HOME belongs to another user."""
    env = {key: value for key in _SAFE_ENV_KEYS if (value := os.environ.get(key))}
    env.update(_FIXED_ENV)
    env["TMPDIR"] = workdir
    if sandboxed:
        env["HOME"] = workdir
    return env


def _effective_uid() -> int:
    return os.geteuid()


def _resolve_sandbox(user: Optional[str]) -> Optional[Tuple[int, int]]:
    """(uid, gid) of the parser user, or None when none is configured. A configured user that is missing, is root, or
    that this process cannot switch to (it is not root) fails the start: never a silent fallback (R3 F3-1)."""
    user = (user or "").strip()
    if not user:
        logger.warning(f"[DriveExtract] no parser user ({PARSER_USER_ENV} unset): parser children run as this "
                       f"process's own user, with its privileges — local development only; the indexer image sets it")
        return None
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        logger.error(f"[DriveExtract] parser user {user!r} does not exist in this container")
        raise RuntimeError(f"parser user {user!r} does not exist in this container") from None
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        logger.error(f"[DriveExtract] parser user {user!r} is root")
        raise RuntimeError(f"parser user {user!r} is root: no sandbox")
    if _effective_uid() != 0:
        logger.error(f"[DriveExtract] switching the parser to {user!r} needs root")
        raise RuntimeError(f"running the parser as {user!r} needs root")
    logger.info(f"[DriveExtract] parser children run as {user} (uid {entry.pw_uid}, gid {entry.pw_gid})")
    return entry.pw_uid, entry.pw_gid


def _collect_window(written: str, wanted: str) -> None:
    """Moves the window the sandboxed child wrote in its directory to where the caller wants it. Only a regular file
    moves; anything else stays behind (removed with the directory), and the caller's own check refuses the cut."""
    try:
        if not stat.S_ISREG(os.lstat(written).st_mode):
            return
        try:
            os.replace(written, wanted)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copyfile(written, wanted)
    except OSError as exc:
        logger.warning(f"[DriveExtract] window not collected: {type(exc).__name__}")


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        logger.debug(f"[DriveExtract] child group {pid} already gone: {type(exc).__name__}")


def _load_result(path: str) -> Optional[Dict[str, Any]]:
    """The child's result: a regular file only, opened without following a link and without blocking on a FIFO — the
    child owns the directory (sandboxed) and could plant either for the parent to read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _RESULT_MAX_BYTES:
            return None
        with os.fdopen(fd, encoding="utf-8", closefd=False) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
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
