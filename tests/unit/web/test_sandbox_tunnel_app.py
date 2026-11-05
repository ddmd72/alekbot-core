"""S5 card B: the tunnel route on ONE HTTP/2 request (S5 design §2.3, §2.5). Real Hypercorn in-process on 127.0.0.1 (h2c,
prior knowledge) and a raw `h2` client; the engine is a scripted fake of the `SandboxTunnelEngine` Protocol."""
import asyncio
import os
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import h2.config
import h2.connection
import h2.events
import h2.settings
import pytest
from h2.errors import ErrorCodes
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus
from src.domain.sandbox_tunnel import (
    BEAT_S, COLLECT_MARGIN_S, END_EOF, IDLE_S, MEDIA_CEILING, RESPONSE_FRAMES, SEND_STALL_S, START_READ_S, Frame,
    FrameReader, FrameType, TunnelEngineResult, TunnelProbe, TunnelResult, TunnelStart, TunnelViolation, TunnelWindow,
    TunnelWindows, decode_result, encode_frame, encode_start, tunnel_budget,
)
from src.web import sandbox_caller_gate, sandbox_tunnel_app
from src.web.sandbox_slot import SandboxSlot
from src.web.sandbox_tunnel_app import SandboxTunnelApp, dispatch_tunnel

SOURCE = 10_000_000
LIMITS = SandboxLimits(MEDIA_CEILING.wall_s, MEDIA_CEILING.memory_bytes, MEDIA_CEILING.cpu_s, MEDIA_CEILING.file_bytes)
PROBE = TunnelStart(op=SandboxOp.MEDIA_PROBE, source_bytes=SOURCE, budget_bytes=tunnel_budget(SOURCE), wall_s=60.0)
WINDOWS = TunnelStart(op=SandboxOp.MEDIA_AUDIO_WINDOWS, source_bytes=SOURCE, budget_bytes=tunnel_budget(SOURCE),
                      wall_s=60.0, container="matroska,webm", duration_known=True,
                      windows=(TunnelWindow(0, 0.0, 1800.0), TunnelWindow(1, 1740.0, 1800.0)))
PROBE_OK = TunnelResult(status=SandboxStatus.OK, media=TunnelProbe(duration_s=1834.5, duration_known=True,
                                                                     has_audio=True, container="mov,mp4,m4a,3gp,3g2,mj2"))
GUARD_S = 2.0          # a hang guard (S4b: without the fix the stream never ends), not a speed threshold


def source_bytes(offset: int, length: int) -> bytes:
    """The recording's bytes: each byte derived from its offset (a cycle of 251, so a misplaced chunk shows)."""
    skip = offset % 251
    return (_CYCLE * (length // 251 + 2))[skip:skip + length]


_CYCLE = bytes(range(251))


def body_frame(kind: FrameType, frame_id: int = 0, value: int = 0, payload: bytes = b"") -> bytes:
    return encode_frame(Frame(kind, frame_id, value, payload))


def start_frame(start: TunnelStart = PROBE) -> bytes:
    return body_frame(FrameType.START, payload=encode_start(start))


def meta_header(**extra: Any) -> bytes:
    return sc.encode_meta(SandboxMeta(request_id=sc.new_request_id(), limits=LIMITS, **extra)).encode()


# ---- the fake engine --------------------------------------------------------------------------------------------

class FakeLink:
    def __init__(self) -> None:
        self.records: "asyncio.Queue[Optional[Frame]]" = asyncio.Queue()
        self.received: List[Frame] = []
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.changed = asyncio.Event()
        self.refuse: Optional[Callable[[Frame], bool]] = None

    async def send(self, frame: Frame) -> None:
        await self.flowing.wait()
        if self.refuse is not None and self.refuse(frame):
            raise TunnelViolation("ledger refused")
        self.received.append(frame)
        self.changed.set()

    async def receive(self) -> Optional[Frame]:
        return await self.records.get()

    def bytes_of(self, stream_id: int) -> bytes:
        return b"".join(f.payload for f in self.received if f.kind is FrameType.DATA and f.id == stream_id)

    def ended(self, stream_id: int) -> bool:
        return any(f.kind is FrameType.END and f.id == stream_id for f in self.received)


class FakeRun:
    def __init__(self, script: Callable[["FakeRun"], Awaitable[TunnelEngineResult]], out_root: Path) -> None:
        self._link = FakeLink()
        self._script = script
        self.out_root = out_root
        self.killed = False
        self.finished = False

    @property
    def link(self) -> FakeLink:
        return self._link

    async def wait(self) -> TunnelEngineResult:
        try:
            result = await self._script(self)
        except asyncio.CancelledError:
            self.killed = True
            raise
        self.finished = True
        return result

    async def open(self, stream_id: int, offset: int) -> None:
        await self._link.records.put(Frame(FrameType.OPEN, stream_id, offset))

    async def close(self, stream_id: int) -> None:
        await self._link.records.put(Frame(FrameType.CLOSE, stream_id))

    async def read_until_end(self, stream_id: int) -> bytes:
        while not self._link.ended(stream_id):
            self._link.changed.clear()
            await self._link.changed.wait()
        return self._link.bytes_of(stream_id)


class FakeEngine:
    def __init__(self, script: Callable[[FakeRun], Awaitable[TunnelEngineResult]], out_root: Path,
                 start_error: Optional[BaseException] = None) -> None:
        self.script = script
        self.out_root = out_root
        self.start_error = start_error
        self.runs: List[FakeRun] = []
        self.calls: List[Tuple[str, SandboxLimits, TunnelStart]] = []

    async def start(self, workdir: str, limits: SandboxLimits, start: TunnelStart) -> FakeRun:
        self.calls.append((workdir, limits, start))
        if self.start_error is not None:
            raise self.start_error
        run = FakeRun(self.script, self.out_root)
        self.runs.append(run)
        return run


def engine_result(result: TunnelResult, windows=(), output_dir=None, opens=0, bytes_in=0) -> TunnelEngineResult:
    return TunnelEngineResult(result=result, windows=tuple(windows), output_dir=output_dir, peak_rss_bytes=4096,
                              wall_s=0.1, opens=opens, bytes_in=bytes_in)


async def two_rounds(run: FakeRun) -> TunnelEngineResult:
    """ffmpeg-like: read the head, then seek: the second OPEN is asked only after the first answer arrived."""
    await run.open(1, 0)
    await run.read_until_end(1)
    await run.close(1)
    await run.open(2, 5_000_000)
    await run.read_until_end(2)
    await run.close(2)
    await run.link.records.put(None)
    return engine_result(PROBE_OK, opens=2)


async def hang(run: FakeRun) -> TunnelEngineResult:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


# ---- the server and the raw h2 client ---------------------------------------------------------------------------

class Recycler:
    def __init__(self) -> None:
        self.calls = 0

    def request_served(self) -> None:
        self.calls += 1


class TokenGate:
    """The gate Protocol: `tok-indexer` may call the media ops; no token is unauthenticated; any other is forbidden."""

    def authorize(self, authorization: Optional[str], op: str):
        from src.web.sandbox_caller_gate import GateDecision
        if authorization is None:
            return GateDecision(False, "unauthenticated")
        if authorization == "Bearer tok-indexer" and op.startswith("media."):
            return GateDecision(True, None)
        return GateDecision(False, "forbidden_op")


async def _inner(scope, receive, send):
    """Stands in for Quart behind the dispatcher: lifespan, and 404 for any other path."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send({"type": "http.response.start", "status": 404, "headers": [(b"x-inner", b"1")]})
    await send({"type": "http.response.body", "body": b"inner", "more_body": False})


class H2Tunnel:
    """A raw h2 client (prior knowledge, h2c): one request stream, the response read concurrently with the body."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.status: Optional[int] = None
        self.headers: Dict[bytes, bytes] = {}
        self.body = b""
        self.frames: "asyncio.Queue[Frame]" = asyncio.Queue()
        self.frame_reader = FrameReader(RESPONSE_FRAMES)
        self.frame_error: Optional[Exception] = None
        self.got_headers = asyncio.Event()
        self.ended = asyncio.Event()
        self.reset = asyncio.Event()
        self.window = asyncio.Event()
        self.sent = 0

    async def open(self, path: str, headers: List[Tuple[bytes, bytes]], body: bytes = b"") -> "H2Tunnel":
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True, header_encoding=None))
        self.conn.initiate_connection()
        self.conn.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 16 * 1024 * 1024})
        self.sid = self.conn.get_next_available_stream_id()
        self.conn.send_headers(self.sid, [(b":method", b"POST"), (b":scheme", b"http"),
                                          (b":authority", b"sandbox.test"), (b":path", path.encode())] + headers)
        self._flush()
        self.task = asyncio.ensure_future(self._read_loop())
        if body:
            await self.send(body)
        return self

    def _flush(self) -> None:
        data = self.conn.data_to_send()
        if data:
            self.writer.write(data)

    async def _read_loop(self) -> None:
        while True:
            data = await self.reader.read(65536)
            if not data:
                self.ended.set()
                return
            for event in self.conn.receive_data(data):
                if isinstance(event, h2.events.ResponseReceived):
                    self.headers = {k: v for k, v in event.headers}
                    self.status = int(self.headers[b":status"])
                    self.got_headers.set()
                elif isinstance(event, h2.events.DataReceived):
                    self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    self.body += event.data
                    if self.status == 200 and self.frame_error is None:
                        try:
                            for frame in self.frame_reader.feed(event.data):
                                self.frames.put_nowait(frame)
                        except TunnelViolation as exc:
                            self.frame_error = exc
                elif isinstance(event, h2.events.StreamEnded):
                    self.ended.set()
                elif isinstance(event, h2.events.StreamReset):
                    self.reset.set()
                    self.ended.set()
                elif isinstance(event, h2.events.WindowUpdated):
                    self.window.set()
            self._flush()

    async def send(self, data: bytes, *, stall_s: float = GUARD_S) -> int:
        """Send within flow control; returns how much went out before the window stayed shut `stall_s`."""
        view = memoryview(data)
        sent = 0
        while view:
            room = min(self.conn.local_flow_control_window(self.sid), self.conn.max_outbound_frame_size, len(view))
            if room <= 0:
                self.window.clear()
                try:
                    await asyncio.wait_for(self.window.wait(), stall_s)
                except asyncio.TimeoutError:
                    return sent
                continue
            self.conn.send_data(self.sid, bytes(view[:room]))
            self._flush()
            await self.writer.drain()
            view = view[room:]
            sent += room
            self.sent += room
        return sent

    def end_body(self) -> None:
        self.conn.end_stream(self.sid)
        self._flush()

    def cancel(self) -> None:
        self.conn.reset_stream(self.sid, ErrorCodes.CANCEL)
        self._flush()

    async def next_frame(self, *, skip_beats: bool = True, timeout: float = GUARD_S) -> Frame:
        while True:
            frame = await asyncio.wait_for(self.frames.get(), timeout)
            if not (skip_beats and frame.kind is FrameType.BEAT):
                return frame

    async def answer(self, frame: Frame, length: int) -> None:
        """The indexer's side of one OPEN: `length` bytes from the offset, then END(eof)."""
        await self.send(body_frame(FrameType.DATA, frame.id, frame.value, source_bytes(frame.value, length)))
        await self.send(body_frame(FrameType.END, frame.id, END_EOF))

    def outcome(self) -> sc.SandboxOutcome:
        return sc.decode_outcome(self.headers[sc.OUTCOME_HEADER.lower().encode()].decode())

    async def close(self) -> None:
        self.task.cancel()
        self.writer.close()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
async def serve_app():
    running: List[Tuple[asyncio.Event, "asyncio.Task[Any]"]] = []

    async def start(app) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        config = HypercornConfig()
        config.bind = [f"fd://{sock.detach()}"]
        config.accesslog = None
        config.graceful_timeout = 0.5
        shutdown = asyncio.Event()
        running.append((shutdown, asyncio.ensure_future(serve(app, config, shutdown_trigger=shutdown.wait))))
        await asyncio.sleep(0)
        return port

    yield start
    for shutdown, task in running:
        shutdown.set()
        try:
            await asyncio.wait_for(task, 5)
        except (asyncio.TimeoutError, Exception):
            task.cancel()


@pytest.fixture
def roots(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir()
    out.mkdir(mode=0o700)
    return work, out


class Rig:
    def __init__(self, port: int, app: SandboxTunnelApp, engine: FakeEngine, recycler: Recycler, slot: SandboxSlot,
                 work: Path, out: Path) -> None:
        self.port, self.app, self.engine, self.recycler, self.slot, self.work, self.out = (
            port, app, engine, recycler, slot, work, out)
        self.clients: List[H2Tunnel] = []

    async def call(self, op: str = "media.probe", *, token: Optional[str] = "tok-indexer",
                   meta: Optional[bytes] = None, body: Optional[bytes] = None) -> H2Tunnel:
        headers = [(b"x-sandbox-meta", meta if meta is not None else meta_header())]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        client = H2Tunnel(self.port)
        self.clients.append(client)
        await client.open(f"/v1/{op}", headers, start_frame() if body is None else body)
        await asyncio.wait_for(client.got_headers.wait(), GUARD_S)
        return client

    async def settled(self) -> None:
        """The request finished on the server (slot freed or recycler told)."""
        for _ in range(200):
            if not self.slot.busy or self.recycler.calls:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the request never finished")


@pytest.fixture
async def rig(serve_app, roots):
    made: List[Rig] = []

    async def build(script=two_rounds, *, gate=None, start_error=None, **knobs) -> Rig:
        work, out = roots
        engine = FakeEngine(script, out, start_error=start_error)
        recycler, slot = Recycler(), SandboxSlot()
        config = {"work_root": str(work), "output_root": str(out), "recycle": "abnormal", "engine_user": None,
                  "slot": slot, "beat_s": 0.05, "idle_s": 5.0, "start_read_s": 0.2, "send_stall_s": 2.0, **knobs}
        app = SandboxTunnelApp({SandboxOp.MEDIA_PROBE: engine, SandboxOp.MEDIA_AUDIO_WINDOWS: engine},
                               gate or TokenGate(), recycler, config)
        port = await serve_app(dispatch_tunnel(_inner, app))
        made.append(Rig(port, app, engine, recycler, slot, work, out))
        return made[-1]

    yield build
    for r in made:
        for client in r.clients:
            await client.close()


# ---- the stream -------------------------------------------------------------------------------------------------

async def test_full_duplex_two_rounds_then_result_and_end_stream(rig):
    r = await rig()
    client = await r.call()
    assert client.status == 200
    assert (client.outcome().status, client.outcome().detail) == (SandboxStatus.OK, "tunnel")

    first = await client.next_frame()
    assert (first.kind, first.id, first.value) == (FrameType.OPEN, 1, 0)
    await client.answer(first, 4096)
    assert (await client.next_frame()).kind is FrameType.CLOSE
    second = await client.next_frame()       # asked only after the first answer arrived, on the same open response
    assert (second.kind, second.id, second.value) == (FrameType.OPEN, 2, 5_000_000)
    await client.answer(second, 2048)
    assert (await client.next_frame()).kind is FrameType.CLOSE
    result = await client.next_frame()
    assert result.kind is FrameType.RESULT and decode_result(result.payload, PROBE) == PROBE_OK
    client.end_body()
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    assert not client.reset.is_set() and client.frame_error is None

    link = r.engine.runs[0].link
    assert link.bytes_of(1) == source_bytes(0, 4096) and link.bytes_of(2) == source_bytes(5_000_000, 2048)
    await r.settled()
    assert r.recycler.calls == 0 and not r.slot.busy and not r.slot.draining     # `ok` keeps the instance warm
    workdir, limits, start = r.engine.calls[0]
    assert start == PROBE and limits == LIMITS and not os.path.exists(workdir)


async def test_windows_are_streamed_as_window_frames_before_the_result(rig):
    async def windows(run: FakeRun) -> TunnelEngineResult:
        out_dir = run.out_root / "media_out_test"
        out_dir.mkdir()
        files = []
        for index, size in ((0, 300_000), (1, 70_000)):
            path = out_dir / f"window_{index}.opus"
            path.write_bytes(bytes([index + 7]) * size)
            files.append((index, str(path)))
        await run.link.records.put(None)
        media = TunnelWindows(status=SandboxStatus.OK, windows=WINDOWS.windows, eof=True, duration_s=1834.5,
                              reached_bound=False)
        return engine_result(TunnelResult(status=SandboxStatus.OK, media=media), files, str(out_dir))

    r = await rig(windows)
    client = await r.call("media.audio_windows", body=start_frame(WINDOWS))
    w0, w1, result = (await client.next_frame(), await client.next_frame(), await client.next_frame())
    assert (w0.kind, w0.id, w0.payload) == (FrameType.WINDOW, 0, bytes([7]) * 300_000)
    assert (w1.kind, w1.id, w1.payload) == (FrameType.WINDOW, 1, bytes([8]) * 70_000)
    assert decode_result(result.payload, WINDOWS).media.windows == WINDOWS.windows
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert not (r.out / "media_out_test").exists()                    # the route removes the engine's copies


async def test_an_engine_raising_mid_stream_still_gets_result_error_and_end_stream(rig):
    """S4b: Hypercorn drops a stream silently when the app raises after the response started."""
    async def raises(run: FakeRun) -> TunnelEngineResult:
        await run.open(1, 0)
        await asyncio.sleep(0.05)
        raise RuntimeError("engine broke")

    r = await rig(raises)
    client = await r.call()
    assert (await client.next_frame()).kind is FrameType.OPEN
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE) == TunnelResult(status=SandboxStatus.ERROR, detail="engine_failure")
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    assert not client.reset.is_set()
    await r.settled()
    assert r.recycler.calls == 1 and r.slot.draining                 # abnormal: the instance ends


async def test_an_unexpected_failure_after_the_response_started_still_ends_the_stream(rig, monkeypatch):
    """The top-level catch (S4b): any exception past `http.response.start` ends the response, RESULT `error` first."""
    real = sandbox_tunnel_app.encode_result
    calls = []

    def fails_once(result, start):
        calls.append(result.status)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return real(result, start)

    monkeypatch.setattr(sandbox_tunnel_app, "encode_result", fails_once)
    r = await rig()
    client = await r.call()
    for _ in range(2):
        frame = await client.next_frame()
        await client.answer(frame, 100)
        await client.next_frame()
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE) == TunnelResult(status=SandboxStatus.ERROR, detail="internal_error")
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert r.recycler.calls == 1


async def test_an_engine_start_failure_is_a_result_error_on_the_opened_stream(rig):
    r = await rig(start_error=OSError("no child"))
    client = await r.call()
    assert client.status == 200
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE).status is SandboxStatus.ERROR
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert r.recycler.calls == 1


async def test_a_frame_violation_kills_the_child_and_ends_with_tunnel_bounds(rig):
    r = await rig(hang)
    client = await r.call()
    await asyncio.sleep(0.05)
    await client.send(start_frame())                                # a second START: outside the contract
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE) == TunnelResult(status=SandboxStatus.ERROR, detail="tunnel_bounds")
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert r.engine.runs[0].killed and r.recycler.calls == 1


async def test_a_frame_the_engines_ledger_refuses_ends_with_tunnel_bounds(rig):
    async def opens(run: FakeRun) -> TunnelEngineResult:
        run.link.refuse = lambda frame: frame.kind is FrameType.DATA and frame.value != 0
        await run.open(1, 0)
        return await hang(run)

    r = await rig(opens)
    client = await r.call()
    first = await client.next_frame()
    await client.send(body_frame(FrameType.DATA, first.id, 999, b"x"))   # an offset the ledger does not expect
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE).detail == "tunnel_bounds"
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert r.engine.runs[0].killed


async def test_a_caller_that_resets_the_stream_kills_the_child_and_recycles(rig):
    async def opens(run: FakeRun) -> TunnelEngineResult:
        await run.open(1, 0)
        return await hang(run)

    r = await rig(opens)
    client = await r.call()
    assert (await client.next_frame()).kind is FrameType.OPEN
    client.cancel()
    await r.settled()
    assert r.engine.runs[0].killed and r.recycler.calls == 1 and r.slot.draining


async def test_beats_flow_while_the_child_works_silently(rig):
    async def quiet(run: FakeRun) -> TunnelEngineResult:
        await asyncio.sleep(0.25)
        await run.link.records.put(None)
        return engine_result(PROBE_OK)

    r = await rig(quiet)
    client = await r.call()
    beats = 0
    while True:
        frame = await client.next_frame(skip_beats=False)
        if frame.kind is FrameType.RESULT:
            break
        assert frame.kind is FrameType.BEAT
        beats += 1
    assert beats >= 3                                                  # every 0.05 s here, BEAT_S in production
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)


def test_production_timings_are_the_contracts(roots):
    work, out = roots
    app = SandboxTunnelApp({}, TokenGate(), Recycler(), {"work_root": str(work), "output_root": str(out)})
    assert (app.beat_s, app.idle_s, app.start_read_s, app.send_stall_s, app.collect_margin_s) == (
        BEAT_S, IDLE_S, START_READ_S, SEND_STALL_S, COLLECT_MARGIN_S) == (5.0, 30.0, 10.0, 30.0, 15.0)
    assert app.policy == "always"


async def test_a_stalled_child_stops_the_callers_body_through_flow_control(rig):
    """Backpressure: while the child does not read, the server stops `receive()`, so what the caller can push is
    bounded by h2 windows and Hypercorn's app queue, never by the body's size."""
    async def stalls(run: FakeRun) -> TunnelEngineResult:
        await run.open(1, 0)
        run.link.flowing.clear()
        data = await run.read_until_end(1)
        await run.close(1)
        await run.link.records.put(None)
        assert data == source_bytes(0, len(data))
        return engine_result(PROBE_OK)

    r = await rig(stalls)
    client = await r.call()
    first = await client.next_frame()
    chunk = 256 * 1024
    total = 6 * chunk                                                  # 1.5 MiB offered
    payload = b"".join(body_frame(FrameType.DATA, 1, off, source_bytes(off, chunk)) for off in range(0, total, chunk))
    accepted = await client.send(payload, stall_s=0.3)
    assert accepted < 1024 * 1024                                      # the window shut: the caller is held back
    r.engine.runs[0].link.flowing.set()
    assert await client.send(payload[accepted:]) == len(payload) - accepted
    await client.send(body_frame(FrameType.END, first.id, END_EOF))
    frame = await client.next_frame()
    while frame.kind is not FrameType.RESULT:
        frame = await client.next_frame()
    assert decode_result(frame.payload, PROBE) == PROBE_OK
    assert r.engine.runs[0].link.bytes_of(1) == source_bytes(0, total)


async def test_a_body_silent_while_the_child_waits_for_bytes_is_tunnel_idle(rig):
    async def opens(run: FakeRun) -> TunnelEngineResult:
        await run.open(1, 0)
        return await hang(run)

    r = await rig(opens, idle_s=0.2)
    client = await r.call()
    assert (await client.next_frame()).kind is FrameType.OPEN
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE).detail == "tunnel_idle"
    client.end_body()                                                 # the caller closes first, on RESULT
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert r.engine.runs[0].killed


async def test_the_run_deadline_kills_the_child_and_answers_timeout(rig):
    short = TunnelStart(op=SandboxOp.MEDIA_PROBE, source_bytes=SOURCE, budget_bytes=tunnel_budget(SOURCE), wall_s=0.2)
    r = await rig(hang, collect_margin_s=0.1)
    client = await r.call(body=start_frame(short))
    result = await client.next_frame()
    assert decode_result(result.payload, short) == TunnelResult(status=SandboxStatus.TIMEOUT, detail="deadline")
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    await r.settled()
    assert r.engine.runs[0].killed and r.recycler.calls == 1


async def test_a_process_left_under_the_engine_uid_recycles_even_after_ok(rig, monkeypatch):
    monkeypatch.setattr(sandbox_tunnel_app, "resolve_engine_ids", lambda user: (os.getuid(), os.getgid()))
    killed = []
    monkeypatch.setattr(sandbox_tunnel_app, "kill_uid_processes", lambda uid: killed.append(uid) or 1)
    r = await rig()
    client = await r.call()
    for _ in range(2):
        frame = await client.next_frame()
        await client.answer(frame, 100)
        await client.next_frame()
    result = await client.next_frame()
    assert decode_result(result.payload, PROBE) == PROBE_OK
    client.end_body()                                                 # the caller closes first, on RESULT
    await r.settled()
    assert killed == [os.getuid()] and r.recycler.calls == 1


# ---- before the stream ------------------------------------------------------------------------------------------

async def test_no_token_is_401_and_a_token_for_another_op_family_is_403(rig):
    r = await rig()
    anonymous = await r.call(token=None)
    assert anonymous.status == 401 and anonymous.outcome().detail == "unauthenticated"
    wrong = await r.call(token="tok-main")
    assert wrong.status == 403 and wrong.outcome().detail == "forbidden_op"
    assert r.engine.calls == [] and not r.slot.busy and r.recycler.calls == 0


@pytest.mark.parametrize("member", ["tok-indexer", "tok-main"])
async def test_the_real_gate_gives_both_light_groups_media_and_refuses_a_caller_in_no_group(rig, monkeypatch, member):
    # S5b: the main service gained `media` next to the indexer, so no light group lacks it; a verified caller in no
    # group is the refusal the real table still makes.
    from src.composition.sandbox_setup import LIGHT_GROUP_PREFIXES

    monkeypatch.delenv("K_SERVICE", raising=False)
    emails = {"Bearer tok-indexer": "indexer@example.com", "Bearer tok-main": "main@example.com",
              "Bearer tok-outsider": "outsider@example.com"}
    monkeypatch.setattr(sandbox_caller_gate, "verify_oidc_identity", lambda auth, audiences: emails.get(auth))
    gate = sandbox_caller_gate.SandboxCallerGate(
        {"MAIN": frozenset({"main@example.com"}), "INDEXER": frozenset({"indexer@example.com"})},
        LIGHT_GROUP_PREFIXES, "https://sandbox-light.test")
    r = await rig(gate=gate)
    refused = await r.call(token="tok-outsider")
    assert refused.status == 403 and refused.outcome().detail == "forbidden_op" and r.engine.calls == []
    allowed = await r.call(token=member)
    assert allowed.status == 200 and (await allowed.next_frame()).kind is FrameType.OPEN


async def test_a_busy_instance_answers_503_recycling_before_streaming(rig):
    r = await rig()
    assert r.slot.try_acquire()                                       # e.g. a parse request in flight
    client = await r.call()
    assert client.status == 503 and client.outcome().status is SandboxStatus.RECYCLING
    await asyncio.wait_for(client.ended.wait(), GUARD_S)
    assert r.engine.calls == [] and r.recycler.calls == 0


@pytest.mark.parametrize("body", [
    body_frame(FrameType.DATA, 1, 0, b"x") + start_frame(),                                  # START not first
    body_frame(FrameType.START, payload=b'{"source_bytes":1,"budget_bytes":1,"wall_s":1,"url":"https://x.test/a"}'),
    start_frame(WINDOWS),                                                                     # the other op's START
    body_frame(FrameType.OPEN, 1, 0),                                                        # a response frame
])
async def test_a_bad_start_is_400_before_streaming(rig, body):
    r = await rig()
    client = await r.call(body=body)
    assert client.status == 400 and client.outcome().detail == "bad_start"
    await r.settled()
    assert r.engine.calls == [] and r.recycler.calls == 0 and not os.listdir(r.work)


async def test_a_body_that_ends_before_start_is_400(rig):
    r = await rig()
    client = H2Tunnel(r.port)
    r.clients.append(client)
    await client.open("/v1/media.probe", [(b"x-sandbox-meta", meta_header()), (b"authorization", b"Bearer tok-indexer")],
                      start_frame()[:10])
    client.end_body()
    await asyncio.wait_for(client.got_headers.wait(), GUARD_S)
    assert client.status == 400 and client.outcome().detail == "bad_start"


async def test_no_start_in_time_is_408(rig):
    r = await rig()
    client = await r.call(body=start_frame()[:5])
    assert client.status == 408 and client.outcome().detail == "start_timeout"


@pytest.mark.parametrize("meta", [b"not-base64-json", None])
async def test_bad_meta_or_meta_carrying_parse_fields_is_400(rig, meta):
    r = await rig()
    client = await r.call(meta=meta if meta is not None else meta_header(kind="mp4"))
    assert client.status == 400 and client.outcome().detail == "bad_meta"
    assert r.engine.calls == []


async def test_an_unknown_media_op_is_404(rig):
    r = await rig()
    client = await r.call("media.video_probe")
    assert client.status == 404 and client.outcome().detail == "unknown_op"
    assert r.engine.calls == [] and not r.slot.busy


async def test_the_dispatcher_sends_only_media_paths_to_the_tunnel(rig):
    r = await rig()
    other = await r.call("parse.extract")
    assert other.status == 404 and other.headers.get(b"x-inner") == b"1"
    tunnel = await r.call("media.probe")
    assert tunnel.status == 200


async def test_get_on_a_tunnel_path_is_405(serve_app, roots):
    work, out = roots
    app = SandboxTunnelApp({}, TokenGate(), Recycler(), {"work_root": str(work), "output_root": str(out)})
    sent: List[Dict[str, Any]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "GET", "path": "/v1/media.probe", "headers": [], "http_version": "2"},
              receive, send)
    assert sent[0]["status"] == 405 and (b"allow", b"POST") in sent[0]["headers"]


def test_the_tunnel_app_refuses_a_non_tunnel_op(roots):
    work, out = roots
    with pytest.raises(ValueError):
        SandboxTunnelApp({SandboxOp.PARSE_EXTRACT: object()}, TokenGate(), Recycler(), {"work_root": str(work)})
