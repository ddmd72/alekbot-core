"""A scripted (and, when a test wants it, hostile) HTTP/2 server on loopback for the media tunnel client tests.

Raw `h2`, h2c with prior knowledge, no ASGI framework: a script drives each accepted connection through `Peer` and sees
exactly what the client wrote (`raw_in`, every byte off the socket; `headers`; `body`; the decoded body frames) and how
the client ended the stream (`client_reset`, `client_ended`). It is not card B's server: it plays the sandbox's side of
the protocol as a test needs it, right or wrong.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Awaitable, Callable, Dict, List, Optional

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions

from src.domain import sandbox_contract as sc
from src.domain.sandbox_tunnel import (
    BODY_FRAMES,
    FRAME_HEADER,
    Frame,
    FrameReader,
    FrameType,
    TunnelProbe,
    TunnelResult,
    TunnelStart,
    TunnelViolation,
    TunnelWindow,
    TunnelWindows,
    decode_start,
    encode_frame,
    encode_result,
)

TUNNEL_OUTCOME = sc.encode_outcome(sc.SandboxOutcome(sc.SandboxStatus.OK, "tunnel"))
Script = Callable[["Peer"], Awaitable[None]]


def outcome(status: sc.SandboxStatus, detail: str = "") -> str:
    return sc.encode_outcome(sc.SandboxOutcome(status, detail))


class Peer:
    """One accepted connection, as the script sees it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, ack: bool = True) -> None:
        self.reader = reader
        self.writer = writer
        self.ack = ack
        self.conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False,
                                                                                header_encoding="utf-8"))
        self.raw_in = bytearray()
        self.headers: Dict[str, str] = {}
        self.body = bytearray()
        self.body_frames: List[Frame] = []
        self.body_violation: Optional[str] = None
        self.frames: "asyncio.Queue[Frame]" = asyncio.Queue()
        self.sid: Optional[int] = None
        self.request = asyncio.Event()
        self.client_ended = asyncio.Event()
        self.gone = asyncio.Event()
        self.window = asyncio.Event()
        self.client_reset: Optional[int] = None
        self.reset_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self._reader = FrameReader(BODY_FRAMES)

    # -- the connection --------------------------------------------------------------------------------------------

    async def pump(self) -> None:
        self.conn.initiate_connection()
        self._flush()
        try:
            while True:
                data = await self.reader.read(1 << 16)
                if not data:
                    break
                self.raw_in += data
                try:
                    events = self.conn.receive_data(data)
                except h2.exceptions.ProtocolError:
                    break
                for event in events:
                    self._on_event(event)
                self._flush()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self.gone.set()
            self.window.set()

    def _on_event(self, event: h2.events.Event) -> None:
        loop = asyncio.get_running_loop()
        if isinstance(event, h2.events.RequestReceived):
            self.sid = event.stream_id
            self.headers = {k: v for k, v in event.headers}
            self.request.set()
        elif isinstance(event, h2.events.DataReceived):
            if self.ack:
                self.conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            self.body += event.data
            try:
                for frame in self._reader.feed(event.data):
                    self.body_frames.append(frame)
                    self.frames.put_nowait(frame)
            except TunnelViolation as exc:
                self.body_violation = str(exc)
        elif isinstance(event, h2.events.StreamEnded):
            self.ended_at = loop.time()
            self.client_ended.set()
        elif isinstance(event, h2.events.StreamReset):
            self.client_reset = int(event.error_code)
            self.reset_at = loop.time()
            self.gone.set()
        elif isinstance(event, (h2.events.WindowUpdated, h2.events.RemoteSettingsChanged)):
            self.window.set()
        elif isinstance(event, h2.events.ConnectionTerminated):
            self.gone.set()

    def _flush(self) -> None:
        data = self.conn.data_to_send()
        if data and not self.writer.is_closing():
            self.writer.write(data)

    # -- what a script does ----------------------------------------------------------------------------------------

    async def respond(self, status: int = 200, outcome_header: Optional[str] = TUNNEL_OUTCOME, *,
                      end: bool = False) -> None:
        headers = [(":status", str(status))]
        if outcome_header is not None:
            headers.append((sc.OUTCOME_HEADER.lower(), outcome_header))
        assert self.sid is not None
        self.conn.send_headers(self.sid, headers, end_stream=end)
        self._flush()
        await self.writer.drain()

    async def send(self, data: bytes, *, end: bool = False) -> None:
        assert self.sid is not None
        view = memoryview(data)
        while view:
            room = min(self.conn.local_flow_control_window(self.sid), self.conn.max_outbound_frame_size, len(view))
            if room <= 0:
                self.window.clear()
                await asyncio.wait_for(self.window.wait(), timeout=5)
                if self.gone.is_set():
                    return
                continue
            self.conn.send_data(self.sid, bytes(view[:room]))
            view = view[room:]
            self._flush()
        if end:
            self.conn.end_stream(self.sid)
            self._flush()
        await self.writer.drain()

    async def frame(self, kind: FrameType, frame_id: int = 0, value: int = 0, payload: bytes = b"", *,
                    end: bool = False) -> None:
        await self.send(encode_frame(Frame(kind, frame_id, value, payload)), end=end)

    async def header_only(self, kind: int, frame_id: int, value: int, length: int) -> None:
        """A frame header with no payload behind it (a declared length is all a size check needs)."""
        await self.send(FRAME_HEADER.pack(kind, frame_id, value, length))

    async def end(self) -> None:
        assert self.sid is not None
        self.conn.end_stream(self.sid)
        self._flush()
        await self.writer.drain()

    def reset(self, code: h2.errors.ErrorCodes = h2.errors.ErrorCodes.INTERNAL_ERROR) -> None:
        assert self.sid is not None
        self.conn.reset_stream(self.sid, error_code=code)
        self._flush()

    def goaway(self) -> None:
        self.conn.close_connection()
        self._flush()

    async def next_frame(self, timeout: float = 5.0) -> Frame:
        return await asyncio.wait_for(self.frames.get(), timeout=timeout)

    async def start(self, op: sc.SandboxOp) -> TunnelStart:
        frame = await self.next_frame()
        assert frame.kind is FrameType.START
        return decode_start(frame.payload, op)

    async def serve_stream(self, stream_id: int, offset: int, size: int) -> bytes:
        """OPEN `stream_id` at `offset`, collect its DATA until END, CLOSE it; the bytes received."""
        await self.frame(FrameType.OPEN, stream_id, offset)
        got = bytearray()
        while True:
            frame = await self.next_frame()
            if frame.kind is FrameType.DATA and frame.id == stream_id:
                assert frame.value == offset + len(got)
                got += frame.payload
            elif frame.kind is FrameType.END and frame.id == stream_id:
                break
        await self.frame(FrameType.CLOSE, stream_id)
        assert offset + len(got) <= size
        return bytes(got)

    async def wait_gone(self, timeout: float = 5.0) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.gone.wait(), timeout=timeout)


class FakeTunnelServer:
    """Accepts connections on 127.0.0.1; connection `i` runs `scripts[min(i, len - 1)]`."""

    def __init__(self, *scripts: Script, ack: bool = True) -> None:
        self.scripts = list(scripts)
        self.ack = ack
        self.peers: List[Peer] = []
        self.errors: List[BaseException] = []
        self.url = ""
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: List["asyncio.Task[None]"] = []

    async def __aenter__(self) -> "FakeTunnelServer":
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for peer in self.peers:
            peer.writer.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = Peer(reader, writer, ack=self.ack)
        index = len(self.peers)
        self.peers.append(peer)
        pump = asyncio.create_task(peer.pump())
        self._tasks.append(pump)
        script = self.scripts[min(index, len(self.scripts) - 1)]
        try:
            await asyncio.wait_for(peer.request.wait(), timeout=5)
            await script(peer)
            await peer.wait_gone()
        except (asyncio.CancelledError, ConnectionError):
            pass
        except BaseException as exc:   # a script's own failure is the test's to see
            if not peer.gone.is_set():
                self.errors.append(exc)
        finally:
            with contextlib.suppress(Exception):
                writer.close()


# -- RESULT payloads ---------------------------------------------------------------------------------------------------

def probe_result(start: TunnelStart, *, duration_s: float = 1834.5, container: str = "mov,mp4,m4a,3gp,3g2,mj2") -> bytes:
    return encode_result(TunnelResult(status=sc.SandboxStatus.OK, media=TunnelProbe(
        duration_s=duration_s, duration_known=True, has_audio=True, container=container)), start)


def windows_result(start: TunnelStart, indexes: List[int], *, status: sc.SandboxStatus = sc.SandboxStatus.OK,
                   eof: bool = False, duration_s: float = 0.0, detail: str = "") -> bytes:
    plan = {w.index: w for w in start.windows}
    produced = tuple(TunnelWindow(index=i, start_s=plan[i].start_s, duration_s=plan[i].duration_s - 0.5)
                     for i in indexes)
    return encode_result(TunnelResult(status=sc.SandboxStatus.OK, media=TunnelWindows(
        status=status, windows=produced, eof=eof, duration_s=duration_s, reached_bound=False, detail=detail)), start)


def failure_result(start: TunnelStart, status: sc.SandboxStatus, detail: str = "") -> bytes:
    return encode_result(TunnelResult(status=status, detail=detail), start)
