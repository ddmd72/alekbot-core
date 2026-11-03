"""HttpMediaStreamAdapter over a loopback aiohttp server (the unit suite's network guard allows loopback only)."""
import asyncio
import logging

import pytest
from aiohttp import web

from src.adapters.http_media_stream_adapter import HttpMediaStreamAdapter
from src.domain.drive_media_source import MediaSource
from src.domain.exceptions import MediaStreamFailed
from src.domain.user_drive import DriveItemNotFoundError

BODY = bytes(range(256)) * 1024          # 256 KiB: several chunks
SECRET_PATH = "/files/item-fa0137/alt-media-secret-path"
BEARER = "Bearer upstream-secret-token"


class Upstream:
    def __init__(self) -> None:
        self.seen_headers = []
        self.closed = asyncio.Event()
        self.status = 200

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.seen_headers.append(dict(request.headers))
        if self.status != 200:
            return web.Response(status=self.status, text="nope")
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for start in range(0, len(BODY), 32 * 1024):
                await response.write(BODY[start:start + 32 * 1024])
                await asyncio.sleep(0.01)
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            self.closed.set()
            raise
        return response


@pytest.fixture
async def upstream():
    up = Upstream()
    app = web.Application()
    app.router.add_get(SECRET_PATH, up.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    up.url = f"http://127.0.0.1:{port}{SECRET_PATH}"
    yield up
    await runner.cleanup()


async def test_streams_in_chunks_with_the_sources_headers(upstream):
    adapter = HttpMediaStreamAdapter(chunk_bytes=16 * 1024)
    chunks = [c async for c in adapter.chunks(MediaSource(url=upstream.url, headers={"Authorization": BEARER}))]
    assert b"".join(chunks) == BODY
    assert len(chunks) >= len(BODY) // (16 * 1024) and max(len(c) for c in chunks) <= 16 * 1024
    assert upstream.seen_headers[0]["Authorization"] == BEARER


@pytest.mark.parametrize("status", [404, 410])
async def test_a_404_is_item_not_found(upstream, status):
    upstream.status = status
    with pytest.raises(DriveItemNotFoundError):
        async for _ in HttpMediaStreamAdapter().chunks(MediaSource(url=upstream.url)):
            pass


async def test_another_status_is_a_failed_stream(upstream):
    upstream.status = 503
    with pytest.raises(MediaStreamFailed):
        async for _ in HttpMediaStreamAdapter().chunks(MediaSource(url=upstream.url)):
            pass


async def test_closing_the_generator_closes_the_upstream(upstream):
    stream = HttpMediaStreamAdapter().chunks(MediaSource(url=upstream.url))
    first = await stream.__anext__()
    assert first
    await stream.aclose()
    await asyncio.wait_for(upstream.closed.wait(), timeout=5)


async def test_no_url_or_header_reaches_an_error_or_a_log(upstream, caplog):
    caplog.set_level(logging.DEBUG)
    errors = []
    upstream.status = 500
    for source in (MediaSource(url=upstream.url, headers={"Authorization": BEARER}),
                   MediaSource(url="http://127.0.0.1:1" + SECRET_PATH, headers={"Authorization": BEARER})):
        with pytest.raises(MediaStreamFailed) as info:
            async for _ in HttpMediaStreamAdapter(connect_timeout_s=2).chunks(source):
                pass
        errors.append(info.value)
    for error in errors:
        assert SECRET_PATH not in str(error) and "secret" not in repr(error)
        assert error.__cause__ is None
    assert "secret" not in caplog.text and SECRET_PATH not in caplog.text
