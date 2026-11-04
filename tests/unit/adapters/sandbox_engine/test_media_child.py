"""The media child's loopback range server (S5 design §2.4), driven in-process: the test plays both ffmpeg (raw HTTP on
127.0.0.1) and the server process (the two tunnel pipes: 13-byte records out, DATA / END frames in). The rules are the
indexer relay's: nonce path, GET/HEAD only, header caps, one range, HEAD from the size with no tunnel traffic, at most
four connections, and the tunnel's own bounds kept by the child so an honest child never trips the engine's ledger."""
import asyncio
import os

import pytest

from src.adapters.sandbox_engine import media_child
from src.adapters.sandbox_engine.media_child import RECORD, RECORD_BYTES, LoopbackRangeServer, serve_in_thread
from src.domain.sandbox_tunnel import (
    END_EOF, END_UPSTREAM_ERROR, MAX_OPEN_STREAMS, MAX_OPENS, Frame, FrameType, encode_frame,
)

_NONCE = "tok-garden-meeting-nonce"
_SOURCE = bytes(range(256)) * 40                    # 10 240 bytes of a recording the child never sees whole
_SIZE = len(_SOURCE)


class Tunnel:
    """The server process's end of the two pipes, on the test's loop."""

    def __init__(self):
        self.data_r, self.data_w = os.pipe()
        self.rec_r, self.rec_w = os.pipe()
        self._reader = None
        self._writer = None

    async def open(self):
        loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(self._reader),
                                     os.fdopen(self.rec_r, "rb", buffering=0))
        transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin,
                                                            os.fdopen(self.data_w, "wb", buffering=0))
        self._writer = asyncio.StreamWriter(transport, protocol, None, loop)

    async def record(self, timeout=5.0):
        raw = await asyncio.wait_for(self._reader.readexactly(RECORD_BYTES), timeout)
        kind, stream_id, offset = RECORD.unpack(raw)
        return FrameType(kind), stream_id, offset

    async def no_record(self, wait=0.2):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(self._reader.readexactly(RECORD_BYTES), wait)

    async def send(self, frame):
        self._writer.write(encode_frame(frame))
        await self._writer.drain()

    def close(self):
        self._writer.transport.abort()
        for fd in (self.data_r, self.rec_w):
            os.close(fd)


@pytest.fixture
async def rig():
    tunnel = Tunnel()
    await tunnel.open()
    server = LoopbackRangeServer(_SIZE, tunnel.data_r, tunnel.rec_w, nonce=_NONCE, idle_s=2.0)
    url = await server.start()
    port = int(url.split(":")[2].split("/")[0])
    yield server, tunnel, port
    await server.close()
    tunnel.close()


async def _connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


async def _request(port, head: bytes, timeout=5.0):
    """Sends one raw request head and reads the whole answer (the server closes every connection)."""
    reader, writer = await _connect(port)
    writer.write(head)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()


def _get(path=f"/{_NONCE}", method="GET", headers=()):
    lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", *headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _status(answer: bytes) -> int:
    return int(answer.split(b" ", 2)[1])


def _header(answer: bytes, name: str) -> str:
    head = answer.split(b"\r\n\r\n", 1)[0].decode()
    for line in head.split("\r\n")[1:]:
        key, _, value = line.partition(":")
        if key.lower() == name.lower():
            return value.strip()
    raise KeyError(name)


def _body(answer: bytes) -> bytes:
    return answer.split(b"\r\n\r\n", 1)[1]


async def test_a_wrong_nonce_is_404_and_opens_nothing(rig):
    _, tunnel, port = rig
    assert _status(await _request(port, _get("/tok-somebody-else"))) == 404
    assert _status(await _request(port, _get(f"/{_NONCE}x"))) == 404
    await tunnel.no_record()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
async def test_only_get_and_head_are_served(rig, method):
    _, tunnel, port = rig
    assert _status(await _request(port, _get(method=method))) == 405
    await tunnel.no_record()


@pytest.mark.parametrize("value", ["bytes=0-1,4-5", "bytes=-500", "bytes=10240-", "bytes=9-3", "items=0-",
                                   "bytes=abc-"])
async def test_a_range_other_than_one_satisfiable_span_is_416(rig, value):
    _, tunnel, port = rig
    answer = await _request(port, _get(headers=[f"Range: {value}"]))
    assert _status(answer) == 416
    assert _header(answer, "Content-Range") == f"bytes */{_SIZE}"
    await tunnel.no_record()


async def test_two_range_headers_are_416(rig):
    _, _, port = rig
    assert _status(await _request(port, _get(headers=["Range: bytes=0-", "Range: bytes=5-"]))) == 416


async def test_more_than_32_headers_is_431(rig):
    _, tunnel, port = rig
    headers = [f"X-Kim-{i}: plot" for i in range(32)]           # + Host = 33
    assert _status(await _request(port, _get(headers=headers))) == 431
    assert _status(await _request(port, _get(method="HEAD", headers=headers[:31]))) == 200   # 32 lines: within
    await tunnel.no_record()


async def test_a_head_over_8_kib_is_431(rig):
    _, tunnel, port = rig
    assert _status(await _request(port, _get(headers=["X-Jordan: " + "a" * 9000]))) == 431
    await tunnel.no_record()


async def test_head_is_answered_from_the_size_with_no_tunnel_traffic(rig):
    _, tunnel, port = rig
    answer = await _request(port, _get(method="HEAD"))
    assert _status(answer) == 200
    assert _header(answer, "Content-Length") == str(_SIZE) and _header(answer, "Accept-Ranges") == "bytes"
    ranged = await _request(port, _get(method="HEAD", headers=["Range: bytes=100-"]))
    assert _status(ranged) == 206 and _header(ranged, "Content-Range") == f"bytes 100-{_SIZE - 1}/{_SIZE}"
    assert _body(ranged) == b""
    await tunnel.no_record()


async def test_a_get_opens_a_stream_and_serves_the_tunnel_bytes(rig):
    _, tunnel, port = rig
    reader, writer = await _connect(port)
    writer.write(_get(headers=["Range: bytes=10000-"]))
    await writer.drain()
    assert await tunnel.record() == (FrameType.OPEN, 1, 10_000)
    await tunnel.send(Frame(FrameType.DATA, 1, 10_000, _SOURCE[10_000:10_100]))
    await tunnel.send(Frame(FrameType.DATA, 1, 10_100, _SOURCE[10_100:]))
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert _status(answer) == 206
    assert _header(answer, "Content-Range") == f"bytes 10000-{_SIZE - 1}/{_SIZE}"
    assert _header(answer, "Content-Length") == str(_SIZE - 10_000)
    assert _body(answer) == _SOURCE[10_000:]
    assert await tunnel.record() == (FrameType.CLOSE, 1, 0)


async def test_a_bounded_range_takes_only_its_bytes_and_closes_the_stream(rig):
    _, tunnel, port = rig
    reader, writer = await _connect(port)
    writer.write(_get(headers=["Range: bytes=2-5"]))
    await writer.drain()
    assert await tunnel.record() == (FrameType.OPEN, 1, 2)
    await tunnel.send(Frame(FrameType.DATA, 1, 2, _SOURCE[2:500]))
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert _body(answer) == _SOURCE[2:6] and _header(answer, "Content-Length") == "4"
    assert await tunnel.record() == (FrameType.CLOSE, 1, 0)
    await tunnel.send(Frame(FrameType.DATA, 1, 500, _SOURCE[500:600]))   # in flight after CLOSE: dropped
    await tunnel.send(Frame(FrameType.END, 1, END_EOF))


async def test_a_get_without_a_range_is_the_whole_file_with_200(rig):
    _, tunnel, port = rig
    reader, writer = await _connect(port)
    writer.write(_get())
    await writer.drain()
    assert await tunnel.record() == (FrameType.OPEN, 1, 0)
    await tunnel.send(Frame(FrameType.DATA, 1, 0, _SOURCE))
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert _status(answer) == 200 and _body(answer) == _SOURCE


async def test_an_upstream_error_ends_the_response_short_and_closes_the_stream(rig):
    _, tunnel, port = rig
    reader, writer = await _connect(port)
    writer.write(_get(headers=["Range: bytes=0-"]))
    await writer.drain()
    assert await tunnel.record() == (FrameType.OPEN, 1, 0)
    await tunnel.send(Frame(FrameType.DATA, 1, 0, _SOURCE[:1000]))
    await tunnel.send(Frame(FrameType.END, 1, END_UPSTREAM_ERROR))
    answer = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert _status(answer) == 206 and len(_body(answer)) == 1000           # short of Content-Length: ffmpeg reconnects
    assert await tunnel.record() == (FrameType.CLOSE, 1, 0)


async def test_an_idle_tunnel_ends_the_response():
    tunnel = Tunnel()
    await tunnel.open()
    server = LoopbackRangeServer(_SIZE, tunnel.data_r, tunnel.rec_w, nonce=_NONCE, idle_s=0.2)
    port = int((await server.start()).split(":")[2].split("/")[0])
    reader, writer = await _connect(port)
    writer.write(_get())
    await writer.drain()
    assert await tunnel.record() == (FrameType.OPEN, 1, 0)
    try:
        answer = await asyncio.wait_for(reader.read(), 5)
    except ConnectionResetError:
        answer = b""
    writer.close()
    assert answer == b"" or _body(answer) == b""                          # headers at most, never a byte of body
    assert await tunnel.record() == (FrameType.CLOSE, 1, 0)
    await server.close()
    tunnel.close()


async def test_a_dropped_connection_closes_its_stream_and_its_bytes_are_dropped(rig):
    _, tunnel, port = rig
    reader, writer = await _connect(port)
    writer.write(_get())
    await writer.drain()
    assert await tunnel.record() == (FrameType.OPEN, 1, 0)
    writer.transport.abort()
    for offset in range(0, 8192, 1024):           # more than the stream queue holds: the pipe read must not stall
        await tunnel.send(Frame(FrameType.DATA, 1, offset, _SOURCE[offset:offset + 1024]))
    assert await tunnel.record() == (FrameType.CLOSE, 1, 0)
    answer = await _request(port, _get(method="HEAD"))
    assert _status(answer) == 200


async def test_at_most_four_connections(rig):
    _, _, port = rig
    idle = [await _connect(port) for _ in range(media_child.MAX_CONNECTIONS)]
    await asyncio.sleep(0.1)
    reader, writer = await _connect(port)
    writer.write(_get(method="HEAD"))
    try:
        dropped = await asyncio.wait_for(reader.read(), 5)
    except ConnectionResetError:                                          # aborted: a reset, no answer
        dropped = b""
    assert dropped == b""
    writer.close()
    for _, w in idle:
        w.close()
    await asyncio.sleep(0.1)
    assert _status(await _request(port, _get(method="HEAD"))) == 200


async def test_a_third_open_stream_is_refused_with_503(rig):
    _, tunnel, port = rig
    held = []
    for stream_id in range(1, MAX_OPEN_STREAMS + 1):
        reader, writer = await _connect(port)
        writer.write(_get())
        await writer.drain()
        assert await tunnel.record() == (FrameType.OPEN, stream_id, 0)
        held.append(writer)
    assert _status(await _request(port, _get())) == 503
    await tunnel.no_record()
    for writer in held:
        writer.transport.abort()


async def test_opens_past_the_tunnel_cap_are_refused_with_503(rig, monkeypatch):
    server, tunnel, port = rig
    server._opens = MAX_OPENS
    assert _status(await _request(port, _get())) == 503
    await tunnel.no_record()


def test_the_record_is_13_bytes_of_type_id_offset():
    assert RECORD_BYTES == 13
    assert RECORD.pack(FrameType.OPEN, 7, 2 ** 40) == bytes([4, 0, 0, 0, 7, 0, 0, 1, 0, 0, 0, 0, 0])


async def test_serve_in_thread_runs_the_server_on_its_own_loop(tmp_path):
    tunnel = Tunnel()
    await tunnel.open()
    server = LoopbackRangeServer(_SIZE, tunnel.data_r, tunnel.rec_w)
    with serve_in_thread(server) as url:
        assert url.startswith("http://127.0.0.1:") and len(url.rsplit("/", 1)[1]) >= 40
        port = int(url.split(":")[2].split("/")[0])
        path = "/" + url.rsplit("/", 1)[1]
        answer = await _request(port, _get(path=path, method="HEAD"))
        assert _status(answer) == 200
    tunnel.close()


def test_the_child_entry_refuses_missing_descriptors():
    assert media_child.main(["media_child.py"]) == media_child.EXIT_BAD_REQUEST
    assert media_child.main(["media_child.py", "x", "y"]) == media_child.EXIT_BAD_REQUEST


def test_the_child_starts_the_drive_child_by_name():
    assert media_child.DRIVE_CHILD_MODULE == "src.adapters.drive_parser.child"
    source = open(media_child.__file__, encoding="utf-8").read()
    assert "import src.adapters.drive_parser" not in source and "from src.adapters.drive_parser" not in source
