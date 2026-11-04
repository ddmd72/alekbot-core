"""
Sandbox tunnel route (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6, §4.7, §7 S5): `POST /v1/media.<op>` carries the media tunnel
of `src/domain/sandbox_tunnel.py` on ONE HTTP/2 request, full duplex.

A raw ASGI app, not a Quart route: Quart drains a request body into memory without backpressure and caps it with
`MAX_CONTENT_LENGTH` and `BODY_TIMEOUT`, while a tunnel body is open-ended and must stall when the engine's child stops
reading. `dispatch_tunnel(inner, tunnel)` puts it in front of the Quart app: an `http` scope whose path starts with
`/v1/media.` goes here, everything else (lifespan included) to Quart.

Order, as the parse route (`sandbox_app`): method (405) -> caller gate (401 unauthenticated, 403 op not allowed) ->
op (404) -> the shared `SandboxSlot` (503 `recycling`) -> `X-Sandbox-Meta` (400 `bad_meta`; a tunnel op takes only
`request_id` and `limits`) -> START, the first body frame, within START_READ_S (400 `bad_start`, 408 `start_timeout`).
Those are ordinary HTTP answers with the outcome header. Then the workdir is made, the response starts (200, outcome
`ok`/`tunnel`: "stream opened" only) and the engine runs. Three tasks feed it: body frames (DATA, END) to the engine's
link, the link's OPEN/CLOSE to the response, and a BEAT every BEAT_S. After the run: one WINDOW per window file, the
RESULT frame (the verdict), the end of the response.

Every path after the response started ends the response itself, RESULT first: Hypercorn drops a stream silently when
the app raises after the response started (spike S4b), and the caller would wait for its own deadline. A violation
seen here (a frame outside the contract, or the engine's ledger refusing what the caller sent) kills the child and
answers RESULT `error`/`tunnel_bounds`. The route's own deadlines: the run ends by min(limits, START) wall +
COLLECT_MARGIN_S (RESULT `timeout`); a response send that stays blocked SEND_STALL_S means the caller is gone; a body
silent for IDLE_S while a stream the child opened still waits for bytes is `tunnel_idle`.

Recycling as `sandbox_app`: under `always` every request that made its workdir ends the instance; under `abnormal` (the
light service) a request ends it unless RESULT `ok` was sent and the response ended, and an engine-uid process left
after the run ends it too. One log line per request: request id, op, status, wall, opens, input bytes, windows, peak
RSS, recycled. Nothing from START, no engine output.

Config keys: `work_root`, `engine_user`, `recycle`, `output_root`, `slot` (as the blueprint's). `beat_s`, `idle_s`,
`start_read_s`, `send_stall_s`, `collect_margin_s` override the contract's constants for tests only; production passes
none.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
from typing import Any, Awaitable, BinaryIO, Callable, Dict, List, Mapping, MutableMapping, Optional, Set, Tuple

from ..domain.sandbox_contract import (
    META_HEADER,
    OUTCOME_HEADER,
    RESPONSE_CAP_BYTES,
    SandboxContractError,
    SandboxOp,
    SandboxOutcome,
    SandboxStatus,
    clamp_limits,
    decode_meta,
    encode_outcome,
)
from ..domain.sandbox_tunnel import (
    BEAT_S,
    BODY_FRAMES,
    COLLECT_MARGIN_S,
    IDLE_S,
    MEDIA_CEILING,
    SEND_STALL_S,
    START_READ_S,
    TUNNEL_OPS,
    WINDOW_MAX_BYTES,
    Frame,
    FrameReader,
    FrameType,
    SandboxTunnelEngine,
    TunnelEngineResult,
    TunnelLink,
    TunnelResult,
    TunnelStart,
    TunnelViolation,
    decode_start,
    encode_frame,
    encode_frame_header,
    encode_result,
)
from ..utils.logger import logger
from .sandbox_app import (
    RECYCLE_ABNORMAL,
    RECYCLE_ALWAYS,
    RECYCLE_NEVER,
    SandboxGate,
    SandboxRecycler,
    kill_uid_processes,
    removable_output_dir,
    resolve_engine_ids,
)
from .sandbox_slot import SandboxSlot

Scope = MutableMapping[str, Any]
Message = Dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

TUNNEL_PATH_PREFIX = "/v1/media."
OPEN_DETAIL = "tunnel"                 # the outcome header of an opened stream: RESULT decides
DETAIL_BOUNDS = "tunnel_bounds"
DETAIL_IDLE = "tunnel_idle"
DETAIL_DEADLINE = "deadline"
DETAIL_CLIENT_GONE = "client_gone"
DETAIL_ENGINE = "engine_failure"
DETAIL_INTERNAL = "internal_error"
_WINDOW_CHUNK = 256 * 1024
_OUTCOME_KEY = OUTCOME_HEADER.lower().encode("ascii")
_UNKNOWN_OP = "unknown"


def dispatch_tunnel(inner: ASGIApp, tunnel: ASGIApp) -> ASGIApp:
    """`http` requests under TUNNEL_PATH_PREFIX go to `tunnel`; every other scope (lifespan included) to `inner`."""
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith(TUNNEL_PATH_PREFIX):
            await tunnel(scope, receive, send)
        else:
            await inner(scope, receive, send)
    return app


class SandboxTunnelApp:
    """The ASGI app of the tunnel ops. `engines` maps each tunnel op to its `SandboxTunnelEngine`.

    Raises ValueError for a non-tunnel op, an unknown recycle policy, or root without an engine user."""

    def __init__(
        self,
        engines: Mapping[SandboxOp, SandboxTunnelEngine],
        gate: SandboxGate,
        recycler: SandboxRecycler,
        config: Mapping[str, Any],
    ) -> None:
        if any(op not in TUNNEL_OPS for op in engines):
            raise ValueError("the tunnel app serves tunnel ops only")
        self.engines: Dict[SandboxOp, SandboxTunnelEngine] = dict(engines)
        self.gate = gate
        self.recycler = recycler
        self.work_root = str(config["work_root"])
        self.output_root = os.path.normpath(str(config["output_root"])) if config.get("output_root") else None
        self.policy = str(config.get("recycle", RECYCLE_ALWAYS))
        if self.policy not in (RECYCLE_ALWAYS, RECYCLE_NEVER, RECYCLE_ABNORMAL):
            raise ValueError(f"unknown recycle policy {self.policy!r}")
        self.engine_ids = resolve_engine_ids(config.get("engine_user"))
        self.slot: SandboxSlot = config.get("slot") or SandboxSlot()
        self.beat_s = float(config.get("beat_s", BEAT_S))
        self.idle_s = float(config.get("idle_s", IDLE_S))
        self.start_read_s = float(config.get("start_read_s", START_READ_S))
        self.send_stall_s = float(config.get("send_stall_s", SEND_STALL_S))
        self.collect_margin_s = float(config.get("collect_margin_s", COLLECT_MARGIN_S))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return
        await _TunnelCall(self, scope, receive, send).serve()


# ---- one request ---------------------------------------------------------------------------------------------------

class _ClientLost(Exception):
    """A response send stayed blocked past the send-stall deadline: the caller stopped reading."""


class _EngineFault(Exception):
    """The engine broke its side of the Protocol (a frame other than OPEN/CLOSE, a window file it did not own)."""


class _TunnelCall:
    """Per-request state and flow; private to this module."""

    def __init__(self, app: SandboxTunnelApp, scope: Scope, receive: Receive, send: Send) -> None:
        self.app = app
        self.scope = scope
        self.receive = receive
        self._asgi_send = send
        self.op = _UNKNOWN_OP
        self.request_id = "-"
        self.started = time.monotonic()
        self.status: Optional[SandboxStatus] = None
        self.acquired = False
        self.wrote = False
        self.recycles = False
        self.finished = False
        self.response_started = False
        self.result_sent = False
        self.ended = False
        self.client_lost = False                 # a send stalled past SEND_STALL_S: nothing more is sent
        self.frame_broken = False                # a WINDOW ended short: no RESULT may follow
        self.start: Optional[TunnelStart] = None
        self.workdir: Optional[str] = None
        self.output_dir: Optional[str] = None
        self.input_bytes = 0
        self.opens = 0
        self.windows = 0
        self.peak_rss = 0
        self.send_lock = asyncio.Lock()
        self.awaiting: Set[int] = set()          # streams the child opened that still wait for bytes
        self.quiet_since = time.monotonic()      # the last body chunk, forwarded frame or OPEN
        self.pump_receiving = False              # the body pump waits on the caller (not on the child)

    # -- top level -----------------------------------------------------------------------------------------------

    async def serve(self) -> None:
        try:
            await self._serve()
        except asyncio.CancelledError:
            self._mark_abnormal()
            raise
        except BaseException as exc:
            logger.error("[SandboxTunnel] request failed: %s (op=%s)", type(exc).__name__, self.op)
            self._mark_abnormal()
            await self._end_after_failure()
            if not isinstance(exc, Exception):
                raise
        finally:
            self._cleanup()
            self._finish()
            self._log()

    async def _serve(self) -> None:
        app = self.app
        if self.scope.get("method") != "POST":
            await self._refuse(405, SandboxStatus.BAD_REQUEST, "method_not_allowed", extra=[(b"allow", b"POST")])
            return
        op_text = str(self.scope.get("path", ""))[len("/v1/"):]
        headers = _headers(self.scope)
        decision = app.gate.authorize(headers.get("authorization"), op_text)
        if not decision.allowed:
            code = 403 if decision.reason == "forbidden_op" else 401
            await self._refuse(code, SandboxStatus.ERROR, decision.reason or "unauthenticated")
            return
        engine = _engine_for(app.engines, op_text)
        if engine is None:
            await self._refuse(404, SandboxStatus.BAD_REQUEST, "unknown_op")
            return
        self.op = op_text
        if not app.slot.try_acquire():
            await self._refuse(503, SandboxStatus.RECYCLING, "recycling", close=True)
            return
        self.acquired = True

        try:
            meta = decode_meta(headers.get(META_HEADER.lower(), ""))
            if meta.kind is not None or meta.ext is not None or meta.windows is not None:
                raise SandboxContractError("a tunnel op takes its request in START")
            limits = clamp_limits(meta.limits, MEDIA_CEILING)
        except SandboxContractError:
            await self._refuse(400, SandboxStatus.BAD_REQUEST, "bad_meta")
            return
        self.request_id = meta.request_id

        opened = await self._read_start(SandboxOp(op_text))
        if opened is None:
            return
        start, reader, leftover = opened
        self.start = start

        workdir = os.path.join(app.work_root, meta.request_id)
        try:
            os.mkdir(workdir, 0o700)
        except FileExistsError:
            await self._refuse(400, SandboxStatus.BAD_REQUEST, "bad_meta")
            return
        self.workdir = workdir
        # From here on the caller's bytes reach the engine: under `always` this request ends the instance; under
        # `abnormal` only a request that does not end with RESULT `ok` does (`_mark_abnormal`).
        self.wrote = True
        if app.policy == RECYCLE_ALWAYS:
            self.recycles = True
            app.slot.drain()
        if app.engine_ids is not None:
            os.chown(workdir, app.engine_ids[0], app.engine_ids[1])

        await self._send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/octet-stream"),
            (_OUTCOME_KEY, self._outcome(SandboxStatus.OK, OPEN_DETAIL)),
        ]})
        self.response_started = True
        await self._run(engine, workdir, limits, start, reader, leftover)

    # -- before the stream ---------------------------------------------------------------------------------------

    async def _read_start(self, op: SandboxOp) -> Optional[Tuple[TunnelStart, FrameReader, List[Frame]]]:
        """START, the first body frame, within START_READ_S. None when answered (400/408) or the caller left."""
        reader = FrameReader(BODY_FRAMES)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.app.start_read_s
        frames: List[Frame] = []
        while not frames:
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise asyncio.TimeoutError
                message = await asyncio.wait_for(self.receive(), remaining)
            except asyncio.TimeoutError:
                await self._refuse(408, SandboxStatus.BAD_REQUEST, "start_timeout")
                return None
            if message.get("type") == "http.disconnect":
                self.status = SandboxStatus.ERROR
                self.client_lost = True
                return None
            body = message.get("body", b"")
            self.input_bytes += len(body)
            try:
                frames = reader.feed(body)
                if not message.get("more_body", False):
                    reader.end()
                    if not frames:
                        raise TunnelViolation("the body ended before START")
            except TunnelViolation:
                await self._refuse(400, SandboxStatus.BAD_REQUEST, "bad_start")
                return None
        try:
            start = decode_start(frames[0].payload, op)
        except TunnelViolation:
            await self._refuse(400, SandboxStatus.BAD_REQUEST, "bad_start")
            return None
        return start, reader, frames[1:]

    async def _refuse(self, code: int, status: SandboxStatus, detail: str, *, close: bool = False,
                      extra: Optional[List[Tuple[bytes, bytes]]] = None) -> None:
        """An ordinary HTTP answer before the stream opened (no payload reached an engine: no recycle)."""
        headers = [(b"content-type", b"text/plain; charset=utf-8"), (_OUTCOME_KEY, self._outcome(status, detail))]
        if close and str(self.scope.get("http_version", "")).startswith("1"):
            headers.append((b"connection", b"close"))     # HTTP/2 has no Connection header
        headers.extend(extra or [])
        self.status = status
        await self._send({"type": "http.response.start", "status": code, "headers": headers})
        await self._send({"type": "http.response.body", "body": detail.replace("_", " ").encode(),
                          "more_body": False})
        self.ended = True

    # -- the stream ----------------------------------------------------------------------------------------------

    async def _run(self, engine: SandboxTunnelEngine, workdir: str, limits: Any, start: TunnelStart,
                   reader: FrameReader, leftover: List[Frame]) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(limits.wall_s, start.wall_s) + self.app.collect_margin_s
        try:
            run = await asyncio.wait_for(engine.start(workdir, limits, start), max(deadline - loop.time(), 0.001))
        except asyncio.TimeoutError:
            logger.error("[SandboxTunnel] engine start overran the deadline (op=%s)", self.op)
            await self._close_stream(TunnelResult(status=SandboxStatus.TIMEOUT, detail=DETAIL_DEADLINE))
            return
        except Exception as exc:
            logger.error("[SandboxTunnel] engine start raised %s (op=%s)", type(exc).__name__, self.op)
            await self._close_stream(TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_ENGINE))
            return

        wait_task = asyncio.ensure_future(run.wait())
        helpers = [
            asyncio.ensure_future(self._pump_body(reader, leftover, run.link)),
            asyncio.ensure_future(self._pump_records(run.link)),
            asyncio.ensure_future(self._beat()),
        ]
        failure: Optional[TunnelResult] = None
        watched: Set["asyncio.Future[Any]"] = {wait_task, *helpers}
        try:
            while failure is None:
                remaining = deadline - loop.time()
                done: Set["asyncio.Future[Any]"] = set()
                if remaining > 0:
                    done, _ = await asyncio.wait(watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    logger.error("[SandboxTunnel] run passed its deadline (op=%s)", self.op)
                    failure = TunnelResult(status=SandboxStatus.TIMEOUT, detail=DETAIL_DEADLINE)
                    break
                for task in done - {wait_task}:
                    watched.discard(task)
                    failure = failure or self._helper_failure(task)
                if wait_task in done:
                    break
        finally:
            for task in helpers:
                task.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            if not wait_task.done():
                wait_task.cancel()        # kills the child and removes what it left (TunnelRun)
                await asyncio.gather(wait_task, return_exceptions=True)
            self._kill_leftovers()

        if failure is None:
            exc = wait_task.exception()
            if exc is not None:
                logger.error("[SandboxTunnel] engine raised %s (op=%s)", type(exc).__name__, self.op)
                failure = TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_ENGINE)
        if failure is not None:
            await self._close_stream(failure)
            return
        await self._deliver(wait_task.result(), start)

    def _helper_failure(self, task: "asyncio.Future[Any]") -> Optional[TunnelResult]:
        """What a helper's end means: None (the child's side of the link ended), or the failure that ends the run."""
        exc = None if task.cancelled() else task.exception()
        if exc is None and not task.cancelled():
            reason = task.result()
            if reason is None:
                return None
            detail = DETAIL_CLIENT_GONE if reason == DETAIL_CLIENT_GONE else DETAIL_IDLE
            logger.warning("[SandboxTunnel] %s (op=%s)", detail, self.op)
            return TunnelResult(status=SandboxStatus.ERROR, detail=detail)
        if isinstance(exc, TunnelViolation):
            logger.warning("[SandboxTunnel] tunnel violation: %s (op=%s)", exc, self.op)
            return TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_BOUNDS)
        if isinstance(exc, _ClientLost):
            logger.warning("[SandboxTunnel] caller stopped reading (op=%s)", self.op)
            return TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_CLIENT_GONE)
        logger.error("[SandboxTunnel] tunnel task failed: %s (op=%s)",
                     "cancelled" if exc is None else type(exc).__name__, self.op)
        return TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_ENGINE)

    async def _pump_body(self, reader: FrameReader, leftover: List[Frame], link: TunnelLink) -> Optional[str]:
        """Caller -> engine. One `receive()` at a time and only after the previous frames reached the child, so a child
        that stops reading stops the caller through h2 flow control. Returns DETAIL_CLIENT_GONE on disconnect; raises
        TunnelViolation on a frame outside the contract or one the engine's ledger refuses."""
        for frame in leftover:
            await self._forward(frame, link)
        body_open = True
        while True:
            self.pump_receiving = True
            try:
                message = await self.receive()
            finally:
                self.pump_receiving = False
            if message.get("type") == "http.disconnect":
                self.client_lost = True          # nothing can reach the caller any more: no send waits on it
                return DETAIL_CLIENT_GONE
            if not body_open:
                continue
            body = message.get("body", b"")
            self.input_bytes += len(body)
            self.quiet_since = time.monotonic()
            for frame in reader.feed(body):
                await self._forward(frame, link)
            if not message.get("more_body", False):
                reader.end()
                body_open = False

    async def _forward(self, frame: Frame, link: TunnelLink) -> None:
        if frame.kind is FrameType.END:
            self.awaiting.discard(frame.id)
        await link.send(frame)
        self.quiet_since = time.monotonic()

    async def _pump_records(self, link: TunnelLink) -> None:
        """Engine -> caller: the child's OPEN and CLOSE, nothing else."""
        while True:
            frame = await link.receive()
            if frame is None:
                return None
            if frame.kind not in (FrameType.OPEN, FrameType.CLOSE):
                raise _EngineFault("the link gave a frame other than OPEN or CLOSE")
            data = encode_frame(frame)
            if frame.kind is FrameType.OPEN:
                self.opens += 1
                self.awaiting.add(frame.id)
                self.quiet_since = time.monotonic()
            else:
                self.awaiting.discard(frame.id)
            await self._emit(data)

    async def _beat(self) -> Optional[str]:
        beat = encode_frame(Frame(FrameType.BEAT))
        while True:
            await asyncio.sleep(self.app.beat_s)
            if (self.awaiting and self.pump_receiving
                    and time.monotonic() - self.quiet_since > self.app.idle_s):
                return DETAIL_IDLE
            await self._emit(beat)

    # -- after the run -------------------------------------------------------------------------------------------

    async def _deliver(self, result: TunnelEngineResult, start: TunnelStart) -> None:
        self.peak_rss = int(result.peak_rss_bytes)
        if result.output_dir:
            if removable_output_dir(os.path.join(result.output_dir, "window"), self.app.output_root,
                                    self.app.work_root):
                self.output_dir = os.path.normpath(result.output_dir)
            else:
                logger.error("[SandboxTunnel] engine output outside its root, ignored (op=%s)", self.op)
                await self._close_stream(TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_ENGINE))
                return
        try:
            payload = encode_result(result.result, start)
            files = self._open_windows(result)
        except (TunnelViolation, _EngineFault) as exc:
            logger.error("[SandboxTunnel] engine result refused: %s (op=%s)", exc, self.op)
            await self._close_stream(TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_ENGINE))
            return
        try:
            for index, handle, size in files:
                await self._emit_window(index, handle, size)
                self.windows += 1
        finally:
            for _, handle, _ in files:
                handle.close()
        await self._close_stream(result.result, payload)

    def _open_windows(self, result: TunnelEngineResult) -> List[Tuple[int, BinaryIO, int]]:
        """Each window: a single-link regular file directly in the engine's output directory, 1..WINDOW_MAX_BYTES,
        all of them within RESPONSE_CAP_BYTES. Opened before any is streamed, so a refusal costs no frame."""
        files: List[Tuple[int, BinaryIO, int]] = []
        total = 0
        try:
            for index, path in result.windows:
                if self.output_dir is None or os.path.dirname(os.path.normpath(path)) != self.output_dir:
                    raise _EngineFault("a window outside the engine's output directory")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                handle = os.fdopen(fd, "rb")
                info = os.fstat(fd)
                files.append((index, handle, info.st_size))
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= WINDOW_MAX_BYTES:
                    raise _EngineFault("a window file is not a single-link regular file within the cap")
                total += info.st_size
                if total > RESPONSE_CAP_BYTES:
                    raise _EngineFault("the windows exceed the response cap")
        except (OSError, _EngineFault) as exc:
            for _, handle, _ in files:
                handle.close()
            raise _EngineFault(str(exc) if isinstance(exc, _EngineFault) else type(exc).__name__) from None
        return files

    async def _emit_window(self, index: int, handle: BinaryIO, size: int) -> None:
        await self._emit(encode_frame_header(FrameType.WINDOW, index, 0, size))
        left = size
        while left:
            chunk = handle.read(min(_WINDOW_CHUNK, left))
            if not chunk:
                # Mid-frame: no RESULT can follow a broken WINDOW. The caller sees the stream end inside a frame.
                self.frame_broken = True
                raise _EngineFault("a window file shrank while streaming")
            left -= len(chunk)
            await self._emit(chunk)

    async def _close_stream(self, result: TunnelResult, payload: Optional[bytes] = None) -> None:
        """RESULT, then the end of the response. Only RESULT `ok` that reached the end keeps the instance warm."""
        assert self.start is not None
        data = payload if payload is not None else encode_result(result, self.start)
        self.status = result.status
        if result.status is not SandboxStatus.OK:
            self._mark_abnormal()
        if self.client_lost:
            self._mark_abnormal()
            return
        await self._emit(encode_frame(Frame(FrameType.RESULT, payload=data)))
        self.result_sent = True
        await self._end()

    async def _end_after_failure(self) -> None:
        """The top-level catch: whatever happened, the caller gets an end (S4b), RESULT `error` first when possible."""
        try:
            if not self.response_started:
                if not self.ended:
                    await self._refuse(500, SandboxStatus.ERROR, DETAIL_INTERNAL)
                return
            self.status = SandboxStatus.ERROR
            if not self.result_sent and not self.frame_broken and not self.client_lost and self.start is not None:
                data = encode_result(TunnelResult(status=SandboxStatus.ERROR, detail=DETAIL_INTERNAL), self.start)
                await self._emit(encode_frame(Frame(FrameType.RESULT, payload=data)))
                self.result_sent = True
        except Exception as exc:
            logger.error("[SandboxTunnel] could not send the error RESULT: %s", type(exc).__name__)
        await self._end()

    async def _end(self) -> None:
        if self.ended:
            return
        self.ended = True
        if self.client_lost:
            self._mark_abnormal()
            return
        try:
            await self._send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception as exc:
            self._mark_abnormal()
            logger.warning("[SandboxTunnel] the response did not end cleanly: %s (op=%s)", type(exc).__name__, self.op)

    # -- plumbing ------------------------------------------------------------------------------------------------

    async def _emit(self, data: bytes) -> None:
        """One response chunk; frames never interleave (one lock), and a send blocked SEND_STALL_S = caller gone."""
        async with self.send_lock:
            await self._send({"type": "http.response.body", "body": data, "more_body": True})

    async def _send(self, message: Message) -> None:
        if self.client_lost:
            raise _ClientLost()
        try:
            await asyncio.wait_for(self._asgi_send(message), self.app.send_stall_s)
        except asyncio.TimeoutError:
            self.client_lost = True
            raise _ClientLost() from None

    def _outcome(self, status: SandboxStatus, detail: str) -> bytes:
        stats = {"input_bytes": self.input_bytes, "wall_s": round(time.monotonic() - self.started, 3),
                 "peak_rss_bytes": self.peak_rss}
        return encode_outcome(SandboxOutcome(status=status, detail=detail, stats=stats)).encode("ascii")

    def _kill_leftovers(self) -> None:
        ids = self.app.engine_ids
        if self.app.policy == RECYCLE_ABNORMAL and ids is not None and kill_uid_processes(ids[0]):
            logger.error("[SandboxTunnel] a process survived the engine child (op=%s): recycling the instance", self.op)
            self._mark_abnormal()

    def _mark_abnormal(self) -> None:
        if self.app.policy == RECYCLE_ABNORMAL and self.wrote:
            self.recycles = True
            self.app.slot.drain()

    def _finish(self) -> None:
        if self.finished or not self.acquired:
            return
        self.finished = True
        if not self.recycles:
            self.app.slot.release()
            return
        try:
            self.app.recycler.request_served()
        except Exception as exc:
            logger.error("[SandboxTunnel] recycler failed: %s", type(exc).__name__)

    def _cleanup(self) -> None:
        for path in (self.workdir, self.output_dir):
            if not path:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if os.path.exists(path):
                logger.error("[SandboxTunnel] directory not removed: request_id=%s", self.request_id)

    def _log(self) -> None:
        status = self.status or SandboxStatus.ERROR
        (logger.info if status == SandboxStatus.OK else logger.warning)(
            "[SandboxTunnel] request_id=%s op=%s status=%s wall_s=%.3f opens=%d bytes_in=%d windows=%d "
            "peak_rss_bytes=%d recycled=%s",
            self.request_id, self.op, status.value, time.monotonic() - self.started, self.opens, self.input_bytes,
            self.windows, self.peak_rss, str(self.recycles).lower(),
        )


def _headers(scope: Scope) -> Dict[str, str]:
    """Request headers by lower-case name, the first of each; values as latin-1 text (ASGI bytes)."""
    out: Dict[str, str] = {}
    for name, value in scope.get("headers") or []:
        key = bytes(name).decode("latin-1").lower()
        if key not in out:
            out[key] = bytes(value).decode("latin-1")
    return out


def _engine_for(engines: Mapping[SandboxOp, SandboxTunnelEngine], op: str) -> Optional[SandboxTunnelEngine]:
    try:
        return engines.get(SandboxOp(op))
    except ValueError:
        return None
