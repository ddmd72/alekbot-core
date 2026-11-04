"""
MediaEngine: the light service's `media.*` engine (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6-§4.9, §7 S5; S5 design §2.3-§2.4).
One instance per op:

    media.probe            one media child, `{"op": "media_probe"}`: ffprobe through the tunnel
    media.audio_windows    one media child, `{"op": "media_windows"}`: Opus windows (or one pass) through the tunnel

    MediaEngine(launcher, *, op, output_root).start(workdir, limits, start) -> TunnelRun   (`SandboxTunnelEngine`)
    run.link.send(DATA | END frame)       the caller's bytes, to the child after the ledger check
    run.link.receive() -> OPEN | CLOSE | None
    run.wait() -> TunnelEngineResult

The child (`media_child.py`, started by the `ChildLauncher` under the engine uid and `limits_exec`) runs the drive
child's own media code against a loopback range server inside itself. Two pipes, passed with `pass_fds`, are its only
channel to this process: fixed 13-byte OPEN / CLOSE records out of it (`media_child.RECORD`), the caller's DATA / END
frames into it. This process parses nothing else from the child but its result file. Every record and every body frame
goes through one `TunnelLedger` (the stream bounds); a breach either way kills the child and the run ends
`error/tunnel_bounds`. A breach by the child makes `receive()` return None; one by the caller makes `send()` raise
TunnelViolation.

Wall clocks: the launcher kills the child's process group at `min(START wall_s, limits.wall_s)`; the child stops its own
ffmpeg CHILD_WALL_MARGIN_S earlier (the in-process extractor's `max(wall - 10, wall / 2)`), so a slow file ends as the
child's own `timeout` with the windows it finished. The memory, CPU and file limits are the request's (`MEDIA_CEILING`
at most, the route's to clamp).

**The workdir is writable by the untrusted child**: the result file and every reported window are opened relative to
one directory descriptor with O_NOFOLLOW | O_NONBLOCK, `fstat`-ed, and must be single-link regular files within their
cap (the parse engine's discipline). What leaves is a copy in a fresh `mkdtemp` directory under the server-only
`output_root`. Windows that would take the response past `response_cap_bytes` are left out and the run reports
`timeout` with the prefix that fits (the stage resumes at the first missing window).

Status (RESULT): the child's result through `result_from_child` (`ok` with the media; the child's `error` as ERROR with
its detail; a probe's `timeout` as TIMEOUT); a launcher kill TIMEOUT; SIGXCPU TIMEOUT; MemoryError / SIGKILL MEMORY;
anything else ERROR. One WARNING line per failure: op, status, detail, wall, opens, bytes; never START's content.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import os
import resource
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from ...domain.sandbox_contract import RESPONSE_CAP_BYTES, SandboxLimits, SandboxOp, SandboxStatus
from ...domain.sandbox_tunnel import (
    FRAME_HEADER_BYTES, RESULT_MAX_BYTES, TUNNEL_OPS, WINDOW_MAX_BYTES, Frame, FrameType, TunnelEngineResult,
    TunnelLedger, TunnelResult, TunnelRun, TunnelStart, TunnelViolation, TunnelWindow, TunnelWindows, encode_frame,
    result_from_child,
)
from ...utils.logger import logger
from .child_launcher import ChildLauncher, ChildResult
from .media_child import RECORD, RECORD_BYTES

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_child.py")
REQUEST_FILE = "request.json"
RESULT_FILE = "result.json"
CHILD_RESULT_MAX_BYTES = 64 * 1024
CHILD_WALL_MARGIN_S = 10.0
_COPY_CHUNK = 1024 * 1024

# The drive child's exit codes (drive_parser/child.py), mirrored as the parse engine does; a test pins them.
EXIT_MEMORY = 3
EXIT_BAD_REQUEST = 4
# ffmpeg's own threads are not set by these; the allocator arenas are (RLIMIT_AS counts each one's reservation).
CHILD_ENV = {"MALLOC_ARENA_MAX": "2"}

_CHILD_OPS = {SandboxOp.MEDIA_PROBE: "media_probe", SandboxOp.MEDIA_AUDIO_WINDOWS: "media_windows"}
_NO_RESULT = "no valid result"
_BAD_WINDOW = "invalid window file"
BOUNDS_DETAIL = "tunnel_bounds"


class NotRegularFile(OSError):
    """A file in the engine-writable workdir is not a single-link regular file within its cap."""


class MediaEngine:
    def __init__(self, launcher: ChildLauncher, *, op: SandboxOp, output_root: Optional[str] = None,
                 python: str = sys.executable, child_script: str = _CHILD_SCRIPT,
                 response_cap_bytes: int = RESPONSE_CAP_BYTES, window_max_bytes: int = WINDOW_MAX_BYTES) -> None:
        if op not in TUNNEL_OPS:
            raise ValueError(f"not a media op: {op}")
        self._op = op
        self._output_root = _checked_output_root(output_root)
        self._launcher = launcher
        self._python = python
        self._child_script = child_script
        self._response_cap = response_cap_bytes
        self._window_max = window_max_bytes

    async def start(self, workdir: str, limits: SandboxLimits, start: TunnelStart) -> TunnelRun:
        """Starts the child and returns at once; the caller pumps `link` while it awaits `wait()`. A run that could
        not be staged is returned already failed (its `receive()` gives None), never raised."""
        if start.op is not self._op:
            raise ValueError("the START is for another op")
        wall_s = min(limits.wall_s, start.wall_s)
        run = _MediaRun(self._op, start, self._output_root, self._window_max, self._response_cap)
        request = _child_request(self._op, start, workdir, limits, wall_s)
        try:
            await asyncio.to_thread(_write_request, os.path.join(workdir, REQUEST_FILE), request)
        except OSError as exc:
            logger.warning(f"[Sandbox] media request file refused: {type(exc).__name__}")
            run.fail(SandboxStatus.ERROR, "request could not be staged")
            return run
        await run.launch(self._launcher, self._python, self._child_script, workdir,
                         dataclasses.replace(limits, wall_s=wall_s))
        return run


class _MediaLink:
    """The engine's end of the tunnel (`TunnelLink`)."""

    def __init__(self, ledger: TunnelLedger, on_violation: Any) -> None:
        self._ledger = ledger
        self._on_violation = on_violation
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._ended = False

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer

    def end(self) -> None:
        """The child is gone: later frames are dropped, `receive()` gives None."""
        self._ended = True

    async def send(self, frame: Frame) -> None:
        async with self._lock:
            if self._ended or self._writer is None:
                return
            try:
                if frame.kind is FrameType.DATA:
                    deliver = self._ledger.data(frame.id, frame.value, len(frame.payload))
                elif frame.kind is FrameType.END:
                    deliver = self._ledger.end(frame.id)
                else:
                    raise TunnelViolation(f"{frame.kind.name} is not a frame for the child")
                raw = encode_frame(frame)
            except TunnelViolation as exc:
                self._on_violation(f"caller: {exc}")
                raise
            if not deliver:
                return                       # a closed stream's bytes in flight: counted, dropped
            try:
                self._writer.write(raw)
                await self._writer.drain()
            except (ConnectionError, OSError):
                return                       # the child is gone; its exit decides the run

    async def receive(self) -> Optional[Frame]:
        if self._ended or self._reader is None:
            return None
        try:
            raw = await self._reader.readexactly(RECORD_BYTES)
        except asyncio.IncompleteReadError as exc:
            if exc.partial and not self._ended:
                self._on_violation("child: a partial record")
            return None
        except (ConnectionError, OSError):
            return None
        code, stream_id, value = RECORD.unpack(raw)
        try:
            if code == FrameType.OPEN:
                self._ledger.open(stream_id, value)
                return Frame(FrameType.OPEN, stream_id, value)
            if code == FrameType.CLOSE and value == 0:
                self._ledger.close(stream_id)
                return Frame(FrameType.CLOSE, stream_id)
            raise TunnelViolation("a record that is neither OPEN nor CLOSE")
        except TunnelViolation as exc:
            self._on_violation(f"child: {exc}")
            return None


class _MediaRun:
    """One child (`TunnelRun`)."""

    def __init__(self, op: SandboxOp, start: TunnelStart, output_root: str, window_max: int, response_cap: int) -> None:
        self._op = op
        self._start = start
        self._output_root = output_root
        self._window_max = window_max
        self._response_cap = response_cap
        self._ledger = TunnelLedger(start.source_bytes, start.budget_bytes)
        self._link = _MediaLink(self._ledger, self._violation)
        self._started = time.monotonic()
        self._task: Optional[asyncio.Task[ChildResult]] = None
        self._failure: Optional[Tuple[SandboxStatus, str]] = None
        self._violated = False
        self._fds: List[int] = []            # this process's copies of the child's pipe ends
        self._transports: List[asyncio.BaseTransport] = []
        self._result: Optional[TunnelEngineResult] = None
        self._workdir = ""
        self._wall_s = start.wall_s

    @property
    def link(self) -> _MediaLink:
        return self._link

    def fail(self, status: SandboxStatus, detail: str) -> None:
        self._failure = (status, detail)
        self._link.end()

    async def launch(self, launcher: ChildLauncher, python: str, script: str, workdir: str,
                     limits: SandboxLimits) -> None:
        self._workdir = workdir
        self._wall_s = limits.wall_s
        data_r, data_w = os.pipe()
        rec_r, rec_w = os.pipe()
        self._fds = [data_r, rec_w]
        loop = asyncio.get_running_loop()
        try:
            reader = asyncio.StreamReader()
            read_transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(rec_r, "rb", buffering=0))
            self._transports.append(read_transport)
            write_transport, protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin, os.fdopen(data_w, "wb", buffering=0))
            self._transports.append(write_transport)
        except OSError as exc:
            logger.warning(f"[Sandbox] media tunnel pipes refused: {type(exc).__name__}")
            self._close_pipes()
            self.fail(SandboxStatus.ERROR, "tunnel could not be staged")
            return
        self._link.attach(reader, asyncio.StreamWriter(write_transport, protocol, None, loop))
        self._task = loop.create_task(launcher.run(
            [python, "-B", "-I", script, str(data_r), str(rec_w)], workdir=workdir, env=CHILD_ENV, limits=limits,
            stdin_path=os.path.join(workdir, REQUEST_FILE), stdout_path=os.devnull,
            op=self._op.value.replace(".", "_"), pass_fds=(data_r, rec_w)))

    async def wait(self) -> TunnelEngineResult:
        if self._result is not None:
            return self._result
        child: Optional[ChildResult] = None
        try:
            if self._task is not None:
                await asyncio.wait({self._task})
        except asyncio.CancelledError:
            await self._kill()
            self._close_pipes()
            raise
        self._link.end()
        self._close_pipes()
        if self._task is not None and not self._task.cancelled():
            exc = self._task.exception()
            if exc is not None:
                logger.warning(f"[Sandbox] media child could not run: {type(exc).__name__}")
                self._failure = self._failure or (SandboxStatus.ERROR, "child could not be started")
            else:
                child = self._task.result()
        if self._violated:
            self._failure = (SandboxStatus.ERROR, BOUNDS_DETAIL)
        result, windows, output_dir = await self._outcome(child)
        wall_s = time.monotonic() - self._started
        if result.status is not SandboxStatus.OK or (
                isinstance(result.media, TunnelWindows) and result.media.status is not SandboxStatus.OK):
            detail = result.detail or (result.media.detail if isinstance(result.media, TunnelWindows) else "")
            logger.warning(f"[Sandbox] media op={self._op.value} status={result.status.value} detail={detail} "
                           f"wall_s={wall_s:.3f} opens={self._ledger.opens} bytes_in={self._ledger.bytes_in}")
        self._result = TunnelEngineResult(
            result=result, windows=windows, output_dir=output_dir,
            peak_rss_bytes=child.peak_rss_bytes if child is not None else _children_peak_rss_bytes(),
            wall_s=wall_s, opens=self._ledger.opens, bytes_in=self._ledger.bytes_in)
        return self._result

    # -- outcome ------------------------------------------------------------------------------------------------------

    async def _outcome(self, child: Optional[ChildResult]
                       ) -> Tuple[TunnelResult, Tuple[Tuple[int, str], ...], Optional[str]]:
        if self._failure is not None:
            return TunnelResult(status=self._failure[0], detail=self._failure[1]), (), None
        if child is None:
            return TunnelResult(status=SandboxStatus.ERROR, detail="child could not be started"), (), None
        failed = _exit_failure(child, self._wall_s)
        if failed is not None:
            return TunnelResult(status=failed[0], detail=failed[1]), (), None
        raw = await asyncio.to_thread(_read_result, self._workdir)
        try:
            if raw is None:
                raise TunnelViolation(_NO_RESULT)
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise TunnelViolation(_NO_RESULT)
            result = result_from_child(parsed, self._start)
        except (TunnelViolation, ValueError):
            return TunnelResult(status=SandboxStatus.ERROR, detail=_NO_RESULT), (), None
        if not isinstance(result.media, TunnelWindows) or not result.media.windows:
            return result, (), None
        return await self._collect(result, result.media)

    async def _collect(self, result: TunnelResult, media: TunnelWindows
                       ) -> Tuple[TunnelResult, Tuple[Tuple[int, str], ...], Optional[str]]:
        out_dir = tempfile.mkdtemp(prefix="media_out_", dir=self._output_root)
        job = asyncio.ensure_future(asyncio.to_thread(
            _copy_windows, self._workdir, [w.index for w in media.windows], out_dir, self._window_max,
            self._response_cap - FRAME_HEADER_BYTES - RESULT_MAX_BYTES))
        try:
            copied = await asyncio.shield(job)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await job
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        except OSError as exc:
            logger.warning(f"[Sandbox] media window file refused: {type(exc).__name__}")
            shutil.rmtree(out_dir, ignore_errors=True)
            return TunnelResult(status=SandboxStatus.ERROR, detail=_BAD_WINDOW), (), None
        if len(copied) < len(media.windows):    # the response cap: the prefix that fits, as a timeout
            kept: Tuple[TunnelWindow, ...] = media.windows[:len(copied)]
            media = TunnelWindows(status=SandboxStatus.TIMEOUT, windows=kept, eof=False, duration_s=0.0,
                                  reached_bound=False, detail="response cap")
            result = TunnelResult(status=SandboxStatus.OK, media=media)
        if not copied:
            shutil.rmtree(out_dir, ignore_errors=True)
            return result, (), None
        return result, tuple(copied), out_dir

    # -- child --------------------------------------------------------------------------------------------------------

    def _violation(self, reason: str) -> None:
        """A breach of the stream bounds: kill the child (its group, by the launcher) and end the link."""
        if not self._violated:
            self._violated = True
            logger.warning(f"[Sandbox] media op={self._op.value} tunnel bounds: {reason}")
        self._link.end()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _kill(self) -> None:
        self._link.end()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait({self._task})

    def _close_pipes(self) -> None:
        """This process's copies of the child's ends first, then its own: a send blocked on a full pipe then fails
        at once instead of waiting for a reader that is gone."""
        for fd in self._fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds = []
        for transport in self._transports:
            if isinstance(transport, asyncio.WriteTransport):
                transport.abort()
            else:
                transport.close()
        self._transports = []


# -- helpers --------------------------------------------------------------------------------------------------------

def _child_request(op: SandboxOp, start: TunnelStart, workdir: str, limits: SandboxLimits,
                   wall_s: float) -> Dict[str, Any]:
    """What the drive child's `serve` and `media.run_*` read; the URL is the child's own. No name, id or path of the
    recording exists to put here (START has none)."""
    request: Dict[str, Any] = {
        "op": _CHILD_OPS[op], "source_bytes": start.source_bytes,
        "out": os.path.join(workdir, RESULT_FILE), "out_dir": workdir,
        "memory_bytes": limits.memory_bytes, "cpu_s": math.ceil(limits.cpu_s), "file_bytes": limits.file_bytes,
        "wall_s": max(wall_s - CHILD_WALL_MARGIN_S, wall_s * 0.5),
    }
    if op is SandboxOp.MEDIA_AUDIO_WINDOWS:
        request["container"] = start.container
        request["duration_known"] = start.duration_known
        request["windows"] = [{"index": w.index, "start_s": w.start_s, "duration_s": w.duration_s}
                              for w in start.windows]
    return request


def _exit_failure(child: ChildResult, wall_s: float) -> Optional[Tuple[SandboxStatus, str]]:
    """The parse engine's exit map (`parse_engine._raise_for_exit`), as (status, detail); None for exit 0."""
    code = child.returncode
    if child.timed_out:
        return SandboxStatus.TIMEOUT, f"wall clock {wall_s:g}s"
    if code == 0:
        return None
    if code == -signal.SIGXCPU:
        return SandboxStatus.TIMEOUT, "cpu limit"
    if code == EXIT_MEMORY or code == -signal.SIGKILL:
        return SandboxStatus.MEMORY, "MemoryError" if code == EXIT_MEMORY else "killed"
    if code == EXIT_BAD_REQUEST:
        return SandboxStatus.ERROR, "child refused the request"
    return SandboxStatus.ERROR, f"exit {code}"


def _children_peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)   # Linux reports KiB, macOS bytes


# -- files (blocking: callers run them off the event loop) ----------------------------------------------------------

def _checked_output_root(output_root: Optional[str]) -> str:
    if output_root is None:
        return tempfile.mkdtemp(prefix="sandbox_out_")   # 0700, owned by this process
    info = os.stat(output_root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        logger.error("[Sandbox] output root is not a directory owned by this process and closed to group/other")
        raise ValueError("output_root must be a directory owned by this process, not group/world-writable")
    return output_root


def _write_request(path: str, request: Dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps(request).encode("utf-8"))


def _open_workdir(workdir: str) -> int:
    return os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_regular(name: str, dir_fd: int, max_bytes: int) -> Tuple[int, int]:
    """(fd, size) of a non-empty single-link regular file within `max_bytes`, opened without following a link or
    blocking on a FIFO."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= max_bytes:
            raise NotRegularFile(name)
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


def _read_result(workdir: str) -> Optional[bytes]:
    try:
        dir_fd = _open_workdir(workdir)
    except OSError as exc:
        logger.warning(f"[Sandbox] media workdir refused: {type(exc).__name__}")
        return None
    try:
        fd, _ = _open_regular(RESULT_FILE, dir_fd, CHILD_RESULT_MAX_BYTES)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(CHILD_RESULT_MAX_BYTES + 1)


def window_file(index: int) -> str:
    """The name `drive_parser.media.window_name` gives a window (mirrored, not imported: REQ-ARCH-23)."""
    return f"window_{index}.opus"


def _copy_windows(workdir: str, indexes: List[int], out_dir: str, window_max: int,
                  budget: int) -> List[Tuple[int, str]]:
    """Copies the reported windows, in order, into `out_dir`. Stops (without error) before the first window whose
    frame would take the response past `budget`; raises OSError (NotRegularFile included) on any refused file."""
    copied: List[Tuple[int, str]] = []
    used = 0
    dir_fd = _open_workdir(workdir)
    try:
        for index in indexes:
            name = window_file(index)
            fd, size = _open_regular(name, dir_fd, window_max)
            with os.fdopen(fd, "rb") as source:
                used += FRAME_HEADER_BYTES + size
                if used > budget:
                    break
                target = os.path.join(out_dir, name)
                sink_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(sink_fd, "wb") as sink:
                    done = 0
                    while chunk := source.read(min(_COPY_CHUNK, size + 1 - done)):
                        sink.write(chunk)
                        done += len(chunk)
                if done != size:
                    raise NotRegularFile(name)
                copied.append((index, target))
    finally:
        os.close(dir_fd)
    return copied
