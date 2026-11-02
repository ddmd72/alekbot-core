"""
LoopbackMediaRelay — a one-use loopback relay in the indexer process, so a media source that needs request headers
(Google's `alt=media` + bearer, GOOGLE_DRIVE_RFC §5 G2, E24) is read by the parser child without any header entering it.

The child gets `http://127.0.0.1:<port>/<nonce>` and nothing else. The relay answers only `GET`/`HEAD` of that path and
streams the one upstream URL fixed at open, adding the source's own headers; the child can choose neither the URL nor a
header. It lives for one run: `relay_child_url` (a `ChildUrlOpener`) opens it around the child and closes it when the
child ends; a timer closes it at the run's wall plus a margin even if nobody calls `close`.

Public interface:
    LoopbackMediaRelay(source, wall_s, *, max_bytes, ssl_context, read_timeout_s)
        .start() -> str, .close() -> None (idempotent)
    relay_child_url(source, wall_s) -> async context manager yielding the child's URL
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Dict, Optional, Set
import ssl

import aiohttp
from aiohttp import web
from aiohttp.web_protocol import RequestHandler

from src.adapters.drive_parser.media import NETWORK_TIMEOUT_US
from src.domain.drive_index_params import TRANSCRIBE_MAX_BYTES
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaUnreadable
from src.utils.logger import logger

RELAY_GRACE_S = 30.0                      # the relay outlives the run's wall by this much, then closes itself
MAX_REQUEST_HEADERS = 32
MAX_REQUEST_HEADER_BYTES = 8 * 1024
MAX_UPSTREAM_STREAMS = 4
MAX_CONNECTIONS = 16                      # the child needs a few (ffmpeg reconnects per seek); more is a flood
_PRE_HANDLER_BYTES = 2 * MAX_REQUEST_HEADER_BYTES   # bytes a connection may send before its handler starts
_CHUNK = 64 * 1024
_RANGE = re.compile(r"bytes=[0-9]*-[0-9]*(,[0-9]*-[0-9]*)*")
_RETURNED = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")


class _BoundedHandler(RequestHandler):
    """aiohttp's parser never enforces a header COUNT (3.13: `max_headers` is accepted and unused) and buffers a request
    until its head is complete, so the byte volume per request is capped here, before the parser sees the data."""

    pending_bytes = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        server = self._manager
        if isinstance(server, _BoundedServer):
            server.open_connections += 1
            if server.open_connections > MAX_CONNECTIONS:
                transport.abort()

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        server = self._manager
        if isinstance(server, _BoundedServer):
            server.open_connections -= 1
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        self.pending_bytes += len(data)
        if self.pending_bytes > _PRE_HANDLER_BYTES:
            if self.transport is not None:
                self.transport.abort()
            return
        super().data_received(data)


class _BoundedServer(web.Server):
    open_connections = 0

    def __call__(self) -> RequestHandler:
        return _BoundedHandler(self, loop=self._loop, **self._kwargs)


class LoopbackMediaRelay:
    def __init__(self, source: MediaSource, wall_s: float, *, max_bytes: int = TRANSCRIBE_MAX_BYTES,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 read_timeout_s: float = NETWORK_TIMEOUT_US / 1_000_000) -> None:
        if not source.url.startswith("https://"):
            raise MediaUnreadable("media url is not https")
        self._url: Optional[str] = source.url
        self._headers: Optional[Dict[str, str]] = dict(source.headers)
        self._wall_s = wall_s
        self._max_bytes = max_bytes
        self._ssl = ssl_context
        self._read_timeout = read_timeout_s
        self._nonce = secrets.token_urlsafe(32)
        self._streams = asyncio.Semaphore(MAX_UPSTREAM_STREAMS)
        self._bytes = 0
        self._closed = False
        self._server: Optional[web.Server] = None
        self._listener: Optional[asyncio.AbstractServer] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._timer: Optional[asyncio.TimerHandle] = None
        self._close_tasks: Set[asyncio.Task[None]] = set()   # strong references: a pending close is never collected
        self._tasks: Set[asyncio.Task[object]] = set()
        self._upstreams: Set[aiohttp.ClientResponse] = set()
        self._transports: Set[asyncio.BaseTransport] = set()

    async def start(self) -> str:
        """Binds 127.0.0.1 on a free port and returns the URL for the child."""
        loop = asyncio.get_running_loop()
        self._session = aiohttp.ClientSession(
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, connect=self._read_timeout,
                                                                 sock_read=self._read_timeout),
            connector=aiohttp.TCPConnector(ssl=self._ssl if self._ssl is not None else True, limit=MAX_UPSTREAM_STREAMS))
        self._server = _BoundedServer(self._handle, max_headers=MAX_REQUEST_HEADERS, max_field_size=MAX_REQUEST_HEADER_BYTES,
                                 max_line_size=MAX_REQUEST_HEADER_BYTES, access_log=None)   # the access log would hold the nonce
        try:
            self._listener = await loop.create_server(self._server, "127.0.0.1", 0)
        except BaseException:
            await self._session.close()
            raise
        port = self._listener.sockets[0].getsockname()[1]
        self._timer = loop.call_later(self._wall_s + RELAY_GRACE_S, self._on_timer)
        return f"http://127.0.0.1:{port}/{self._nonce}"

    async def close(self) -> None:
        """Aborts the in-flight upstream streams at once and drops the headers. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._headers = None
        self._url = None
        if self._timer is not None:
            self._timer.cancel()
        if self._listener is not None:
            self._listener.close()
        for upstream in list(self._upstreams):
            upstream.close()
        for transport in list(self._transports):
            transport.abort()
        for task in list(self._tasks):
            task.cancel()
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        if self._listener is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._listener.wait_closed(), timeout=1.0)
        logger.info(f"[DriveMedia] relay closed after {self._bytes} bytes")

    # -- internals --------------------------------------------------------------------------------------------------

    def _spawn_close(self) -> None:
        task = asyncio.get_running_loop().create_task(self.close())
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    def _on_timer(self) -> None:
        logger.warning("[DriveMedia] relay closed by its timer: the run never closed it")
        self._spawn_close()

    def _reject(self, status: int, *, close: bool = False) -> web.Response:
        response = web.Response(status=status)
        if close:
            response.force_close()
        return response

    async def _handle(self, request: web.BaseRequest) -> web.StreamResponse:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        transport = request.transport
        if isinstance(request.protocol, _BoundedHandler):
            request.protocol.pending_bytes = 0
        if transport is not None:
            self._transports.add(transport)
        try:
            return await self._serve(request)
        finally:
            if task is not None:
                self._tasks.discard(task)
            if transport is not None:
                self._transports.discard(transport)

    async def _serve(self, request: web.BaseRequest) -> web.StreamResponse:
        raw = request.raw_headers
        if len(raw) > MAX_REQUEST_HEADERS or sum(len(k) + len(v) + 4 for k, v in raw) > MAX_REQUEST_HEADER_BYTES:
            return self._reject(431, close=True)
        if not secrets.compare_digest(request.path[1:].encode("utf-8"), self._nonce.encode("utf-8")) \
                or not request.path.startswith("/"):
            return self._reject(404)
        if request.method not in ("GET", "HEAD"):
            return self._reject(405)
        if request.can_read_body or request.content_length:
            return self._reject(400, close=True)
        if self._closed or self._session is None or self._url is None or self._headers is None:
            return self._reject(503, close=True)
        headers = {"Accept-Encoding": "identity", **self._headers}
        wanted = request.headers.get("Range")
        if wanted is not None and _RANGE.fullmatch(wanted):
            headers["Range"] = wanted
        async with self._streams:
            return await self._relay(request, headers)

    async def _relay(self, request: web.BaseRequest, headers: Dict[str, str]) -> web.StreamResponse:
        session, url = self._session, self._url
        if session is None or url is None:
            return self._reject(503, close=True)
        try:
            upstream = await session.request(request.method, url, headers=headers, allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(f"[DriveMedia] relay upstream failed: {type(exc).__name__}")
            return self._reject(502, close=True)
        self._upstreams.add(upstream)
        try:
            status = upstream.status
            logger.info(f"[DriveMedia] relay upstream {status // 100}xx")
            if 300 <= status < 400:
                return self._reject(502, close=True)
            response = web.StreamResponse(status=status)
            for name in _RETURNED:
                value = upstream.headers.get(name)
                if value is not None:
                    response.headers[name] = value
            await response.prepare(request)
            if request.method == "HEAD":
                await response.write_eof()
                return response
            sent = 0
            try:
                async for chunk in upstream.content.iter_chunked(_CHUNK):
                    self._bytes += len(chunk)
                    if self._bytes > self._max_bytes:
                        logger.warning(f"[DriveMedia] relay reached the byte cap ({self._max_bytes}): closed")
                        self._spawn_close()
                        if request.transport is not None:
                            request.transport.abort()
                        return response
                    await response.write(chunk)
                    sent += len(chunk)
                await response.write_eof()
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.warning(f"[DriveMedia] relay stream ended early after {sent} bytes: {type(exc).__name__}")
                if request.transport is not None:
                    request.transport.abort()
            return response
        finally:
            self._upstreams.discard(upstream)
            upstream.close()


@contextlib.asynccontextmanager
async def relay_child_url(source: MediaSource, wall_s: float) -> AsyncIterator[str]:
    """The `ChildUrlOpener` the composition injects (G2). A header-free `https://` source reaches the child as is; a
    header-carrying one is streamed through a one-use loopback relay closed when the run ends. Anything else is
    refused (MediaUnreadable), logged without url or headers."""
    if not source.url.startswith("https://"):
        logger.error("[DriveMedia] the media source url is not https: refused")
        raise MediaUnreadable("media url is not https")
    if source.header_free:
        yield source.url
        return
    if source.expires_at is not None:
        expires = source.expires_at if source.expires_at.tzinfo else source.expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc) + timedelta(seconds=wall_s + RELAY_GRACE_S):
            logger.warning("[DriveMedia] the media source expires before the run ends: refused")
            raise MediaUnreadable("media source expires within the run")
    relay = LoopbackMediaRelay(source, wall_s)
    url = await relay.start()
    try:
        yield url
    finally:
        await relay.close()
