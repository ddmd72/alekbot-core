"""HttpMediaStreamAdapter — the one-time link's byte stream over aiohttp (DRIVE_MCP_CONNECTOR_RFC §8).

One `ClientSession` per stream, closed when the generator closes (a client disconnect included); 64 KiB chunks; a
connect timeout and a read-stall timeout, no total timeout (a long download is capped by the Cloud Run request
timeout). A source that carries headers (a provider bearer) never follows a redirect, so the headers never reach
another host; a header-free (pre-signed) source may. The precedent is `drive_parser/media_relay.py`. The url and the
headers never reach a log line or an error.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional
import ssl

import aiohttp

from ..domain.drive_media_source import MediaSource
from ..domain.exceptions import MediaStreamFailed
from ..domain.user_drive import DriveItemNotFoundError
from ..ports.media_stream_port import MediaStreamPort
from ..utils.logger import logger

CHUNK_BYTES = 64 * 1024
CONNECT_TIMEOUT_S = 15.0
READ_STALL_TIMEOUT_S = 60.0
_GONE = frozenset({404, 410})


class HttpMediaStreamAdapter(MediaStreamPort):
    def __init__(self, *, ssl_context: Optional[ssl.SSLContext] = None, connect_timeout_s: float = CONNECT_TIMEOUT_S,
                 read_timeout_s: float = READ_STALL_TIMEOUT_S, chunk_bytes: int = CHUNK_BYTES) -> None:
        self._ssl = ssl_context
        self._timeout = aiohttp.ClientTimeout(total=None, connect=connect_timeout_s, sock_read=read_timeout_s)
        self._chunk = chunk_bytes

    async def chunks(self, source: MediaSource) -> AsyncIterator[bytes]:
        connector = aiohttp.TCPConnector(ssl=self._ssl if self._ssl is not None else True)
        async with aiohttp.ClientSession(timeout=self._timeout, connector=connector, auto_decompress=False) as session:
            try:
                async with session.get(source.url, headers=dict(source.headers),
                                       allow_redirects=source.header_free) as upstream:
                    status = upstream.status
                    if status in _GONE:
                        logger.info(f"[DriveLink] upstream {status}")
                        raise DriveItemNotFoundError("the file is gone at its source")
                    if not 200 <= status < 300:
                        logger.warning(f"[DriveLink] upstream {status // 100}xx")
                        raise MediaStreamFailed(f"the source answered {status}")
                    async for chunk in upstream.content.iter_chunked(self._chunk):
                        yield chunk
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # the type only: aiohttp's message can carry the url
                logger.warning(f"[DriveLink] upstream stream failed: {type(exc).__name__}")
                raise MediaStreamFailed(f"the stream failed: {type(exc).__name__}") from None
