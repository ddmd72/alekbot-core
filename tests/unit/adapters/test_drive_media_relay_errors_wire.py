"""The loopback media relay's error paths on the wire (GOOGLE_DRIVE_RFC G2, E24): a request that arrives once the relay
is closed is a 503 with the connection closed, an unreachable or failing upstream is a 502 with the connection closed, and
a stream that breaks mid-body aborts the transport, so the child sees a failed read, never a clean short file. Reuses the
helpers of `test_drive_media_relay_wire.py`; synthetic bytes only."""
import asyncio
import ssl

import aiohttp
import pytest
from aiohttp import web

from src.adapters.drive_parser.media_relay import LoopbackMediaRelay
from src.domain.drive_index_params import TRANSCRIBE_EXTRACT_WALL_S
from src.domain.drive_media_source import MediaSource
from tests.unit.adapters.drive_media_fixtures import make_ca_and_server_cert
from tests.unit.adapters.test_drive_media_relay_wire import TOKEN, Upstream, _get, _relay, _trusting, upstream  # noqa: F401


async def test_a_request_on_a_connection_that_outlived_the_close_is_503_and_closes_the_connection(upstream):
    relay = _relay(upstream)
    url = await relay.start()
    port = int(url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await relay.close()
        writer.write(f"GET {url[url.index('/', 8):]} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.read(4096), 5)
    finally:
        writer.close()
    assert head.startswith(b"HTTP/1.1 503") and b"connection: close" in head.lower()
    assert upstream.seen == []                                          # nothing was fetched after the close


async def test_an_unreachable_upstream_is_502_and_closes_the_connection(upstream):
    port = upstream.port
    await upstream.stop()                                               # nothing listens on the source's port now
    relay = LoopbackMediaRelay(MediaSource(f"https://localhost:{port}/fid-1?alt=media", {"Authorization": TOKEN}),
                               TRANSCRIBE_EXTRACT_WALL_S, ssl_context=_trusting(upstream), read_timeout_s=2.0)
    url = await relay.start()
    try:
        status, headers, body = await _get(url)
        assert status == 502 and body == b"" and headers.get("Connection", "").lower() == "close"
    finally:
        await relay.close()


async def test_an_upstream_that_stalls_past_the_read_timeout_is_502(upstream):
    upstream.hold_s = 1.0
    relay = LoopbackMediaRelay(MediaSource(upstream.url, {"Authorization": TOKEN}),
                               TRANSCRIBE_EXTRACT_WALL_S, ssl_context=_trusting(upstream), read_timeout_s=0.2)
    url = await relay.start()
    try:
        status, headers, _ = await _get(url)
        assert status == 502 and headers.get("Connection", "").lower() == "close"
    finally:
        await relay.close()


async def test_a_stream_that_breaks_mid_body_aborts_the_transport(tmp_path):
    ca, cert, key = make_ca_and_server_cert(tmp_path)

    async def cut(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        response.content_length = 100_000                              # promises far more than it sends
        await response.prepare(request)
        await response.write(b"y" * 1000)
        request.transport.abort()
        return response

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", cut)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]                    # type: ignore[union-attr]
    client_ssl = ssl.create_default_context(cafile=ca)
    client_ssl.verify_flags &= ~ssl.VERIFY_X509_STRICT
    relay = LoopbackMediaRelay(MediaSource(f"https://localhost:{port}/fid-1/Hamburg%20talk.mp4?alt=media",
                                           {"Authorization": TOKEN}), TRANSCRIBE_EXTRACT_WALL_S, ssl_context=client_ssl)
    url = await relay.start()
    try:
        with pytest.raises((aiohttp.ClientPayloadError, aiohttp.ServerDisconnectedError,
                            aiohttp.ClientOSError)):
            async with asyncio.timeout(10):
                await _get(url)                                         # a short body must never read as a whole file
    finally:
        await relay.close()
        await runner.cleanup()
