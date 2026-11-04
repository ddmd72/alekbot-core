"""
Media engine child entry (UNTRUSTED_CONTENT_SANDBOX_RFC §4.6, §7 S5; S5 design §2.4): runs under `limits_exec`, as the
engine uid.

    python -B -I <path>/media_child.py <data_fd> <record_fd>      (the request JSON on stdin)

It runs the drive child's own media code unchanged (`drive_parser/child.py` `serve` + `dispatch`, which hands
`media_probe` / `media_windows` to `drive_parser/media.py`), and gives it a URL that serves the recording: a loopback
range server in this process, `http://127.0.0.1:<port>/<nonce>`, whose bytes come over the tunnel. ffmpeg reads it as it
read the indexer's relay (same argv, allowlists, pass mode, `-xerror` behaviour).

Two pipes, passed by the engine (`pass_fds`), connect this process to the server process and nothing else:
- `record_fd` (child -> server): fixed 13-byte records `u8 type | u32 id | u64 offset` (RECORD), OPEN(id, offset) when
  ffmpeg's connection asks for the recording from `offset`, CLOSE(id) when that connection is done. Nothing else is
  written there: the server never parses HTTP, a URL or a header from this process.
- `data_fd` (server -> child): the caller's DATA / END frames (the tunnel codec), forwarded after the engine's ledger
  check. A frame for an id this process no longer serves is dropped.

The loopback server's rules (the indexer relay's, re-implemented here; an engine module imports no other adapter
subpackage, REQ-ARCH-23): the path is a fresh nonce compared in constant time; GET and HEAD only; the head at most
HEAD_MAX_BYTES and HEADERS_MAX lines; one range `bytes=N-` or `bytes=N-M` (anything else 416); HEAD and the response
headers come from the source size with no tunnel traffic; `206` with `Content-Range`, `Content-Length`, `Accept-Ranges`;
at most MAX_CONNECTIONS connections, at most MAX_OPEN_STREAMS open streams and MAX_OPENS opens (the tunnel's bounds, so
an honest child never trips the engine's ledger: a request past them gets 503). A stream that ends short (END with an
upstream error, or an idle tunnel) closes the socket, so ffmpeg's reconnect asks again with a new OPEN.

The loopback server runs on its own event loop in a thread; ffmpeg runs from the main thread, as the drive child runs
it. The child module is a string, never an import (REQ-ARCH-23), started by file path for the reason `parse_child.py`
gives. Exit status is the drive child's: 0 result written, 3 memory, 4 unreadable request or limits.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import re
import runpy
import secrets
import struct
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from src.domain.sandbox_tunnel import (  # noqa: E402  (after the repo root is on the path: started by file path)
    DATA_MAX, FRAME_HEADER_BYTES, IDLE_S, MAX_OPEN_STREAMS, MAX_OPENS, Frame, FrameReader, FrameType,
    TunnelViolation,
)

DRIVE_CHILD_MODULE = "src.adapters.drive_parser.child"
MEDIA_OPS = ("media_probe", "media_windows")
EXIT_BAD_REQUEST = 4                     # the drive child's: unreadable request

RECORD = struct.Struct(">BIQ")           # child -> server: type (FrameType.OPEN / CLOSE), stream id, offset
RECORD_BYTES = RECORD.size               # 13

HEAD_MAX_BYTES = 8 * 1024
HEADERS_MAX = 32
MAX_CONNECTIONS = 4
QUEUE_FRAMES = 4                         # DATA frames buffered per stream: the pipe stalls behind a slow ffmpeg
_PIPE_READ = FRAME_HEADER_BYTES + DATA_MAX
_RANGE_RE = re.compile(r"bytes=([0-9]{1,19})-([0-9]{0,19})")
_REASONS = {200: "OK", 206: "Partial Content", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
            416: "Range Not Satisfiable", 431: "Request Header Fields Too Large", 503: "Service Unavailable"}


@dataclass
class _Stream:
    queue: "asyncio.Queue[Frame]" = field(default_factory=lambda: asyncio.Queue(QUEUE_FRAMES))
    closed: bool = False


class LoopbackRangeServer:
    """Serves one recording of `source_bytes` bytes on 127.0.0.1 to the media child's ffmpeg, through the tunnel pipes.

    `start()` binds a free port and returns the URL; `close()` stops serving. Runs on one event loop (`serve_in_thread`
    gives it its own thread in the child; the tests drive it on theirs)."""

    def __init__(self, source_bytes: int, data_fd: int, record_fd: int, *, nonce: Optional[str] = None,
                 idle_s: float = IDLE_S) -> None:
        if not isinstance(source_bytes, int) or isinstance(source_bytes, bool) or source_bytes < 1:
            raise ValueError("source size out of range")
        self._size = source_bytes
        self._data_fd = data_fd
        self._record_fd = record_fd
        self._nonce = nonce or secrets.token_urlsafe(32)
        self._idle_s = idle_s
        self._streams: Dict[int, _Stream] = {}
        self._next_id = 1
        self._opens = 0
        self._connections = 0
        self._server: Optional[asyncio.AbstractServer] = None
        self._pipe_task: Optional[asyncio.Task[None]] = None
        self._pipe_transport: Optional[asyncio.BaseTransport] = None
        self._writers: List[asyncio.StreamWriter] = []
        self.url = ""

    @property
    def opens(self) -> int:
        return self._opens

    async def start(self) -> str:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_PIPE_READ)
        self._pipe_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(self._data_fd, "rb", buffering=0, closefd=False))
        self._pipe_task = loop.create_task(self._pump_pipe(reader))
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=HEAD_MAX_BYTES)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/{self._nonce}"
        return self.url

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        if self._pipe_task is not None:
            self._pipe_task.cancel()
            with contextlib.suppress(BaseException):
                await self._pipe_task
        if self._pipe_transport is not None:
            self._pipe_transport.close()

    # -- tunnel side -------------------------------------------------------------------------------------------------

    async def _pump_pipe(self, reader: asyncio.StreamReader) -> None:
        """The server's DATA / END frames, to the stream they belong to. A full stream queue stops this read, so the
        pipe fills and the server stops reading the caller's body: a slow ffmpeg slows the indexer, never the memory."""
        frames = FrameReader(frozenset({FrameType.DATA, FrameType.END}))
        while True:
            chunk = await reader.read(_PIPE_READ)
            if not chunk:
                return
            try:
                batch = frames.feed(chunk)
            except TunnelViolation:          # the engine forwards checked frames only: nothing sane is left to do
                self._abort_all()
                return
            for frame in batch:
                stream = self._streams.get(frame.id)
                if stream is not None and not stream.closed:
                    await stream.queue.put(frame)

    def _record(self, kind: FrameType, stream_id: int, offset: int = 0) -> None:
        """One record to the server. At most 2 x MAX_OPENS records of 13 bytes per run, far below a pipe's buffer, so
        the blocking write never waits."""
        os.write(self._record_fd, RECORD.pack(int(kind), stream_id, offset))

    def _abort_all(self) -> None:
        for writer in list(self._writers):
            writer.transport.abort()

    # -- HTTP side ---------------------------------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connections >= MAX_CONNECTIONS:
            writer.transport.abort()
            return
        self._connections += 1
        self._writers.append(writer)
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.transport.abort()
        finally:
            self._connections -= 1
            self._writers.remove(writer)
            if not writer.transport.is_closing():
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self._idle_s)
        except (asyncio.LimitOverrunError, ValueError):
            await self._answer(writer, 431)
            return
        request = _parse_head(head)
        if request is None:
            await self._answer(writer, 400)
            return
        method, target, headers = request
        if len(headers) > HEADERS_MAX:
            await self._answer(writer, 431)
            return
        if not hmac.compare_digest(target.encode("utf-8", "replace"), f"/{self._nonce}".encode("utf-8")):
            await self._answer(writer, 404)
            return
        if method not in ("GET", "HEAD"):
            await self._answer(writer, 405)
            return
        names = [name for name, _ in headers]
        if "transfer-encoding" in names or any(name == "content-length" and value != "0" for name, value in headers):
            await self._answer(writer, 400)
            return
        ranges = [value for name, value in headers if name == "range"]
        span = self._span(ranges)
        if span is None:
            await self._answer(writer, 416, [("Content-Range", f"bytes */{self._size}")])
            return
        first, last, partial = span
        status = 206 if partial else 200
        fields = [("Accept-Ranges", "bytes"), ("Content-Type", "application/octet-stream"),
                  ("Content-Length", str(last - first + 1))]
        if partial:
            fields.append(("Content-Range", f"bytes {first}-{last}/{self._size}"))
        if method == "HEAD":
            await self._answer(writer, status, fields)
            return
        if len(self._streams) >= MAX_OPEN_STREAMS or self._opens >= MAX_OPENS:
            await self._answer(writer, 503)
            return
        await self._stream(writer, status, fields, first, last)

    def _span(self, ranges: List[str]) -> Optional[Tuple[int, int, bool]]:
        """(first, last, partial) of the one range asked for, the whole file without one; None = not satisfiable."""
        if not ranges:
            return 0, self._size - 1, False
        match = _RANGE_RE.fullmatch(ranges[0].strip()) if len(ranges) == 1 else None
        if match is None:
            return None
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else self._size - 1
        if first >= self._size or last < first:
            return None
        return first, min(last, self._size - 1), True

    async def _stream(self, writer: asyncio.StreamWriter, status: int, fields: List[Tuple[str, str]],
                      first: int, last: int) -> None:
        stream_id = self._next_id
        self._next_id += 1
        self._opens += 1
        stream = _Stream()
        self._streams[stream_id] = stream
        self._record(FrameType.OPEN, stream_id, first)
        try:
            writer.write(_head(status, fields))
            await writer.drain()
            position, remaining = first, last - first + 1
            while remaining > 0:
                frame = await asyncio.wait_for(stream.queue.get(), timeout=self._idle_s)
                if frame.kind is FrameType.END or frame.value != position:
                    break                    # the source ended or failed before the range did: ffmpeg reconnects
                piece = frame.payload[:remaining]
                writer.write(piece)
                await writer.drain()
                position += len(piece)
                remaining -= len(piece)
            if remaining > 0:
                writer.transport.abort()
            else:
                writer.close()
        finally:
            stream.closed = True
            del self._streams[stream_id]
            while not stream.queue.empty():  # frees a pipe read blocked on this queue
                stream.queue.get_nowait()
            self._record(FrameType.CLOSE, stream_id)

    async def _answer(self, writer: asyncio.StreamWriter, status: int,
                      fields: Optional[List[Tuple[str, str]]] = None) -> None:
        if status >= 400:
            fields = [*(fields or []), ("Content-Length", "0")]
        writer.write(_head(status, fields or []))
        await writer.drain()
        writer.close()


def _parse_head(head: bytes) -> Optional[Tuple[str, str, List[Tuple[str, str]]]]:
    """(method, target, [(lower-case name, value)]) of a request head, None when it is not one."""
    try:
        lines = head[:-4].decode("latin-1").split("\r\n")
    except UnicodeDecodeError:
        return None
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    headers: List[Tuple[str, str]] = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            return None
        headers.append((name.lower(), value.strip()))
    return parts[0], parts[1], headers


def _head(status: int, fields: List[Tuple[str, str]]) -> bytes:
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}", *(f"{k}: {v}" for k, v in fields),
             "Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


@contextlib.contextmanager
def serve_in_thread(server: LoopbackRangeServer, *, start_timeout_s: float = 10.0) -> Iterator[str]:
    """Runs `server` on its own event loop in a daemon thread for the block; yields its URL."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    failure: List[BaseException] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.start())
        except BaseException as exc:      # reported to the caller's thread
            failure.append(exc)
            ready.set()
            return
        ready.set()
        loop.run_forever()
        loop.run_until_complete(server.close())

    thread = threading.Thread(target=run, name="media-loopback", daemon=True)
    thread.start()
    if not ready.wait(start_timeout_s) or failure:
        raise RuntimeError("the loopback server did not start") from (failure[0] if failure else None)
    try:
        yield server.url
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)


def _handler(dispatch: Callable[[Dict[str, Any]], Dict[str, Any]], data_fd: int,
             record_fd: int) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handle(request: Dict[str, Any]) -> Dict[str, Any]:
        if request.get("op") not in MEDIA_OPS:
            return {"status": "error", "detail": "not a media op"}
        server = LoopbackRangeServer(int(request["source_bytes"]), data_fd, record_fd)
        with serve_in_thread(server) as url:
            return dispatch({**request, "url": url, "ca_file": ""})
    return handle


def main(argv: List[str]) -> int:
    try:
        data_fd, record_fd = int(argv[1]), int(argv[2])
        os.set_inheritable(data_fd, False)       # never to ffmpeg (which also runs with close_fds)
        os.set_inheritable(record_fd, False)
    except (IndexError, ValueError, OSError):
        sys.stderr.write("media_child: usage: <data_fd> <record_fd>\n")
        return EXIT_BAD_REQUEST
    child = runpy.run_module(DRIVE_CHILD_MODULE)   # run_name is the module's: its own `sys.exit(main())` stays idle
    return int(child["serve"](_handler(child["dispatch"], data_fd, record_fd)))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
