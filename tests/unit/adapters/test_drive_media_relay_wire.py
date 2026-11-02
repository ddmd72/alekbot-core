"""The loopback media relay on the wire (GOOGLE_DRIVE_RFC G2 relay half, E24): a local https upstream stands in for
Google; the relay is driven by a plain http client, as ffmpeg would. Synthetic bytes only."""
import asyncio
import functools
import logging
import socket
import ssl
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp
import pytest
from aiohttp import web

from src.adapters.drive_parser import media_relay
from src.adapters.drive_parser.media_relay import (
    MAX_UPSTREAM_STREAMS, LoopbackMediaRelay, relay_child_url,
)
from src.domain.drive_index_params import TRANSCRIBE_EXTRACT_WALL_S
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaUnreadable
from tests.unit.adapters.drive_media_fixtures import make_ca_and_server_cert

TOKEN = "Bearer tok-a"
DATA = bytes(range(256)) * 40      # "Hamburg talk.mp4", 10 KiB of synthetic bytes
FIVE = {"content-type", "content-length", "content-range", "accept-ranges"}


class Upstream:
    """An https server on localhost recording what reaches it."""

    def __init__(self, ca_pem: str, cert: str, key: str) -> None:
        self.ca_pem, self._cert, self._key = ca_pem, cert, key
        self.seen: List[Dict[str, str]] = []
        self.methods: List[str] = []
        self.hold_s = 0.0
        self.body = DATA
        self.redirect_to: Optional[str] = None
        self.endless = False
        self.running = 0
        self.peak = 0
        self.aborted = asyncio.Event()
        self._runner: Optional[web.AppRunner] = None
        self.port = 0

    async def start(self) -> "Upstream":
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self._cert, self._key)
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]    # type: ignore[union-attr]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}/fid-1/Hamburg%20talk.mp4?alt=media"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.seen.append(dict(request.headers))
        self.methods.append(request.method)
        if self.redirect_to:
            return web.Response(status=302, headers={"Location": self.redirect_to})
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.hold_s:
                await asyncio.sleep(self.hold_s)
            headers = {"Content-Type": "video/mp4", "X-Leak": "tok-a", "Set-Cookie": "sid=tok-a",
                       "Accept-Ranges": "bytes"}
            body = self.body
            status = 200
            wanted = request.headers.get("Range")
            if wanted:
                first, _, last = wanted[6:].partition("-")
                start, end = int(first or 0), int(last) if last else len(body) - 1
                headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                body, status = body[start:end + 1], 206
            response = web.StreamResponse(status=status, headers=headers)
            response.content_length = len(body) if not self.endless else None
            await response.prepare(request)
            if request.method == "HEAD":
                return response
            try:
                if self.endless:
                    while True:
                        await response.write(b"x" * 4096)
                        await asyncio.sleep(0.01)
                await response.write(body)
                await response.write_eof()
            except (ConnectionError, asyncio.CancelledError):
                self.aborted.set()
                raise
            return response
        finally:
            self.running -= 1


@pytest.fixture
async def upstream(tmp_path):
    ca, cert, key = make_ca_and_server_cert(tmp_path)
    server = await Upstream(ca, cert, key).start()
    yield server
    await server.stop()


def _source(server: Upstream, **kwargs) -> MediaSource:
    return MediaSource(server.url, {"Authorization": TOKEN}, **kwargs)


def _trusting(server: Upstream) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=server.ca_pem)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT      # the throwaway CA carries no key identifiers
    return context


def _relay(server: Upstream, wall_s: float = TRANSCRIBE_EXTRACT_WALL_S, **kwargs) -> LoopbackMediaRelay:
    return LoopbackMediaRelay(_source(server), wall_s, ssl_context=_trusting(server), **kwargs)


async def _get(url: str, *, method: str = "GET", headers: Optional[Dict[str, str]] = None, data=None):
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, data=data, allow_redirects=False) as resp:
            return resp.status, dict(resp.headers), await resp.read()


def _port_open(url: str) -> bool:
    port = int(url.split(":")[2].split("/")[0])
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def test_nonce_path_is_served_and_only_five_headers_come_back(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        assert url.startswith("http://127.0.0.1:")
        status, headers, body = await _get(url + "?ignored=1")
        assert (status, body) == (200, DATA)
        assert {k.lower() for k in headers} - {"date", "server"} <= FIVE
        assert "tok-a" not in str(headers) and "x-leak" not in {k.lower() for k in headers}
        head_status, head_headers, head_body = await _get(url, method="HEAD")
        assert head_status == 200 and head_body == b"" and head_headers["Content-Length"] == str(len(DATA))
    finally:
        await relay.close()


async def test_wrong_nonce_and_other_paths_are_404_and_never_reach_upstream(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        base = url.rsplit("/", 1)[0]
        for path in ("/wrong-nonce", "/", "/" + url.rsplit("/", 1)[1] + "/more"):
            assert (await _get(base + path))[0] == 404
        assert upstream.seen == []
    finally:
        await relay.close()


async def test_the_nonce_is_compared_with_compare_digest(upstream, monkeypatch):
    calls = []
    real = media_relay.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(media_relay.secrets, "compare_digest", spy)
    relay = _relay(upstream)
    url = await relay.start()
    try:
        await _get(url)
        assert calls and calls[0][1] == url.rsplit("/", 1)[1].encode()
    finally:
        await relay.close()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_other_methods_are_405(upstream, method):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        assert (await _get(url, method=method))[0] == 405
        assert upstream.seen == []
    finally:
        await relay.close()


async def test_a_request_body_is_refused(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        status, _, _ = await _get(url, data=b"smuggled")
        assert status == 400
        assert upstream.seen == []
    finally:
        await relay.close()


async def test_more_than_32_request_headers_is_431(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        ok, _, _ = await _get(url, headers={f"X-H{i}": "v" for i in range(20)})
        assert ok == 200
        status, _, _ = await _get(url, headers={f"X-H{i}": "v" for i in range(33)})
        assert status == 431
        assert len(upstream.seen) == 1
    finally:
        await relay.close()


async def test_nine_kib_of_request_headers_is_refused_and_the_connection_closed(upstream):
    """One header line over 8 KiB is refused by aiohttp's parser (`max_line_size`/`max_field_size`, security review S1)
    with 400 before the handler runs, and the connection is closed; 8 KiB summed over several lines is the handler's 431."""
    relay = _relay(upstream)
    url = await relay.start()
    try:
        port, path = int(url.split(":")[2].split("/")[0]), "/" + url.split("/", 3)[3]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Big: {'a' * 9 * 1024}\r\n\r\n".encode())
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(), timeout=5)   # read() to EOF: the relay closes the connection
        finally:
            writer.close()
        assert answer.split(b" ", 2)[1] == b"400"   # the parser answers before it knows the version (HTTP/1.0 line)
        assert upstream.seen == []
        status, _, _ = await _get(url, headers={f"X-H{i}": "a" * 1000 for i in range(10)})
        assert status == 431
        assert upstream.seen == []
    finally:
        await relay.close()


async def test_a_valid_range_is_forwarded_and_an_invalid_one_dropped(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        status, headers, body = await _get(url, headers={"Range": "bytes=10-19"})
        assert (status, body) == (206, DATA[10:20]) and headers["Content-Range"] == f"bytes 10-19/{len(DATA)}"
        assert upstream.seen[-1]["Range"] == "bytes=10-19"
        for bad in ("bytes=1-2;evil", "items=1-2", "bytes=a-b", "bytes=1-2\t, x"):
            status, _, body = await _get(url, headers={"Range": bad})
            assert (status, body) == (200, DATA)
            assert "Range" not in upstream.seen[-1]
    finally:
        await relay.close()


async def test_source_headers_reach_upstream_and_nothing_else_of_the_childs(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        _, headers, _ = await _get(url, headers={"Authorization": "Bearer evil", "Cookie": "c=1", "X-Other": "1"})
        sent = upstream.seen[-1]
        assert sent["Authorization"] == TOKEN
        assert "Cookie" not in sent and "X-Other" not in sent
        assert "tok-a" not in str(headers)
    finally:
        await relay.close()


async def test_an_http_upstream_is_refused_at_open(upstream):
    with pytest.raises(MediaUnreadable):
        LoopbackMediaRelay(MediaSource("http://localhost:1/x", {"Authorization": TOKEN}), 60.0)
    with pytest.raises(MediaUnreadable):
        async with relay_child_url(MediaSource("http://localhost:1/x", {"Authorization": TOKEN}), 60.0):
            pass


async def test_a_redirect_is_502_and_its_location_is_never_contacted(upstream):
    contacted = []

    async def evil(request):
        contacted.append(request.path)
        return web.Response(text="evil")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", evil)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    evil_port = site._server.sockets[0].getsockname()[1]    # type: ignore[union-attr]
    upstream.redirect_to = f"http://127.0.0.1:{evil_port}/stolen"
    relay = _relay(upstream)
    url = await relay.start()
    try:
        status, headers, _ = await _get(url)
        assert status == 502 and "Location" not in headers
        assert contacted == []
    finally:
        await relay.close()
        await runner.cleanup()


async def test_close_on_context_exit_stops_listening(upstream, monkeypatch):
    monkeypatch.setattr(media_relay, "LoopbackMediaRelay", functools.partial(
        LoopbackMediaRelay, ssl_context=_trusting(upstream)))
    async with relay_child_url(_source(upstream), TRANSCRIBE_EXTRACT_WALL_S) as url:
        assert url.startswith("http://127.0.0.1:")
        assert (await _get(url))[0] == 200
    assert not _port_open(url)


async def test_close_on_cancellation_aborts_the_in_flight_stream(upstream, monkeypatch):
    monkeypatch.setattr(media_relay, "LoopbackMediaRelay", functools.partial(
        LoopbackMediaRelay, ssl_context=_trusting(upstream)))
    upstream.endless = True
    opened = asyncio.Event()
    urls: List[str] = []

    async def run():
        async with relay_child_url(_source(upstream), TRANSCRIBE_EXTRACT_WALL_S) as url:
            urls.append(url)
            opened.set()
            await asyncio.sleep(60)

    task = asyncio.ensure_future(run())
    await opened.wait()
    session = aiohttp.ClientSession()
    resp = await session.get(urls[0])
    await resp.content.readany()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises((aiohttp.ClientError, asyncio.TimeoutError)):
        async with asyncio.timeout(5):
            while await resp.content.readany():
                pass
    await asyncio.wait_for(upstream.aborted.wait(), 5)
    await session.close()
    assert not _port_open(urls[0])


async def test_the_timer_closes_the_relay_and_aborts_the_stream(upstream, monkeypatch):
    monkeypatch.setattr(media_relay, "RELAY_GRACE_S", 0.0)
    upstream.endless = True
    relay = _relay(upstream, wall_s=0.3)
    url = await relay.start()
    session = aiohttp.ClientSession()
    resp = await session.get(url)
    await resp.content.readany()
    with pytest.raises((aiohttp.ClientError, asyncio.TimeoutError)):
        async with asyncio.timeout(5):
            while await resp.content.readany():
                pass
    await asyncio.wait_for(upstream.aborted.wait(), 5)
    await session.close()
    assert not _port_open(url)
    await relay.close()


async def test_the_byte_cap_closes_the_relay(upstream):
    upstream.body = b"y" * 5000
    relay = _relay(upstream, max_bytes=1000)
    url = await relay.start()
    got = 0
    session = aiohttp.ClientSession()
    try:
        resp = await session.get(url)
        with pytest.raises((aiohttp.ClientError, asyncio.TimeoutError)):
            async with asyncio.timeout(5):
                while chunk := await resp.content.readany():
                    got += len(chunk)
        assert got <= 1000
        await asyncio.sleep(0.2)
        assert not _port_open(url)
    finally:
        await session.close()
        await relay.close()


async def test_an_expiry_inside_the_wall_is_refused(upstream, monkeypatch):
    monkeypatch.setattr(media_relay, "LoopbackMediaRelay", functools.partial(
        LoopbackMediaRelay, ssl_context=_trusting(upstream)))
    soon = datetime.now(timezone.utc) + timedelta(seconds=TRANSCRIBE_EXTRACT_WALL_S + 10)
    with pytest.raises(MediaUnreadable):
        async with relay_child_url(_source(upstream, expires_at=soon), TRANSCRIBE_EXTRACT_WALL_S):
            pytest.fail("the relay must not open")
    later = datetime.now(timezone.utc) + timedelta(seconds=TRANSCRIBE_EXTRACT_WALL_S + 600)
    async with relay_child_url(_source(upstream, expires_at=later), TRANSCRIBE_EXTRACT_WALL_S) as url:
        assert (await _get(url))[0] == 200


async def test_a_header_free_https_source_passes_unchanged_and_http_is_refused():
    free = MediaSource("https://media.example.invalid/fid-1")
    async with relay_child_url(free, 60.0) as url:
        assert url == free.url
    with pytest.raises(MediaUnreadable):
        async with relay_child_url(MediaSource("http://media.example.invalid/fid-1"), 60.0):
            pass


async def test_at_most_four_upstream_streams_at_once(upstream):
    upstream.hold_s = 0.2
    relay = _relay(upstream)
    url = await relay.start()
    try:
        results = await asyncio.gather(*[_get(url) for _ in range(10)])
        assert all(r[0] == 200 for r in results)
        assert 1 < upstream.peak <= MAX_UPSTREAM_STREAMS
    finally:
        await relay.close()


async def test_logs_hold_no_token_nonce_or_url(upstream, caplog):
    caplog.set_level(logging.DEBUG)
    relay = _relay(upstream)
    url = await relay.start()
    nonce = url.rsplit("/", 1)[1]
    await _get(url)
    await _get(url.rsplit("/", 1)[0] + "/nope")
    await _get(url, headers={"Range": "bytes=1-2"})
    await relay.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[DriveMedia]" in text
    for secret in ("tok-a", nonce, upstream.url, "Hamburg", "alt=media", "localhost"):
        assert secret not in text


@pytest.mark.slow
async def test_ffprobe_reads_a_duration_through_the_relay_with_range_seeks(upstream, tmp_path, monkeypatch):
    """A real ffprobe against the relay (moov at the end: it seeks with Range)."""
    import shutil
    import subprocess
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe are not installed")
    from src.adapters.drive_parser import media
    from tests.unit.adapters.drive_media_fixtures import make_audio_mp4
    upstream.body = make_audio_mp4(tmp_path / "Hamburg talk.mp4", 6)
    relay = _relay(upstream)
    url = await relay.start()
    try:
        argv = media.build_probe_argv(url, "/unused/ca.pem")
        done = await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=60)
        assert done.returncode == 0, done.stderr[-300:]
        import json
        duration = float(json.loads(done.stdout)["format"]["duration"])
        assert 5.0 < duration < 7.5
        assert upstream.seen and upstream.seen[0]["Authorization"] == TOKEN
    finally:
        await relay.close()


async def _raw(url: str, request: bytes) -> bytes:
    port = int(url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(4096), 5)
    except ConnectionError:
        return b""                       # aborted by the relay: a refusal too
    finally:
        writer.close()


async def test_s1_the_parser_limits_refuse_oversized_headers_before_the_handler_runs(upstream, monkeypatch):
    """The caps are aiohttp parser limits, not a check after buffering: a flood of headers or one oversized line is
    refused (4xx) while the handler never runs."""
    ran: List[int] = []
    original = LoopbackMediaRelay._serve

    async def spy(self, request):
        ran.append(1)
        return await original(self, request)

    monkeypatch.setattr(LoopbackMediaRelay, "_serve", spy)
    relay = _relay(upstream)
    url = await relay.start()
    try:
        path = url.split("127.0.0.1:")[1].split("/", 1)[1]
        flood = f"GET /{path} HTTP/1.1\r\nHost: x\r\n".encode() + b"".join(f"X-H{i}: v\r\n".encode() for i in range(2000)) + b"\r\n"
        assert (await _raw(url, flood)).split(b"\r\n", 1)[0].split(b" ")[1:2] in ([b"400"], [b"431"], [])
        line = f"GET /{path} HTTP/1.1\r\nHost: x\r\nX-Big: ".encode() + b"a" * 9000 + b"\r\n\r\n"
        assert (await _raw(url, line)).split(b"\r\n", 1)[0].split(b" ")[1:2] in ([b"400"], [b"431"], [])
        assert ran == [] and upstream.seen == []
    finally:
        await relay.close()


async def test_s1_the_server_is_built_with_the_limits():
    from unittest import mock
    with mock.patch.object(media_relay, "_BoundedServer") as server:
        server.return_value = mock.Mock()
        relay = LoopbackMediaRelay(MediaSource("https://example.invalid/x", {}), 10.0)
        try:
            await relay.start()
        except Exception:
            pass
        finally:
            await relay.close()
    kwargs = server.call_args.kwargs
    assert (kwargs["max_headers"], kwargs["max_field_size"], kwargs["max_line_size"]) == (32, 8192, 8192)


async def test_n1_a_unicode_digit_range_is_dropped(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    try:
        status, _, body = await _get(url, headers={"Range": "bytes=٠-١"})
        assert (status, body) == (200, DATA)
        assert "Range" not in upstream.seen[-1]
    finally:
        await relay.close()


async def test_n2_pending_close_tasks_stay_referenced_when_timer_and_cap_both_fire(upstream):
    relay = _relay(upstream)
    await relay.start()
    relay._spawn_close()
    relay._spawn_close()
    assert len(relay._close_tasks) == 2        # neither can be collected while pending
    await asyncio.gather(*relay._close_tasks)
    await asyncio.sleep(0)
    assert relay._close_tasks == set()


async def test_s1_more_connections_than_the_cap_are_aborted(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    port = int(url.split(":")[2].split("/")[0])
    opened = []
    try:
        for _ in range(media_relay.MAX_CONNECTIONS + 4):
            opened.append(await asyncio.open_connection("127.0.0.1", port))
        await asyncio.sleep(0.2)
        dead = 0
        for reader, _writer in opened:
            try:
                if await asyncio.wait_for(reader.read(1), 0.3) == b"":
                    dead += 1
            except asyncio.TimeoutError:
                pass
        assert dead >= 4
    finally:
        for _reader, writer in opened:
            writer.close()
        await relay.close()
