"""
HttpRangeSource: the drive side of the media tunnel (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §7 S5; S5 design
§2.3 "Size" and upstream rules, §2.6).

The one place in the tunnel path where a drive recording's URL and request headers (Google's `alt=media` + bearer, a
OneDrive pre-authenticated URL) are used. They stay in the indexer: the sandbox asks for an offset, never for a URL, and
the bytes travel back as DATA frames. Neither the URL nor a header is ever logged or put into an error; an error says
only what kind of answer came back.

Rules (copied from the loopback relay it replaces): `https://` only; no redirects; `Accept-Encoding: identity` and the
raw bytes (never decoded); the size from one `Range: bytes=0-0` (206 with `Content-Range: bytes 0-0/N`), N at most
`TRANSCRIBE_MAX_BYTES`; each stream one `Range: bytes=<offset>-` GET answered 206 with a `Content-Range` whose start is
the offset and whose total is N, cut at N; a source that expires within the run's wall plus `EXPIRY_GRACE_S` is refused
(no token refresh between ranges).

Public interface:
    HttpRangeSource(source, *, wall_s, transport=None, now=None)
        .size() -> int                    raises MediaUnreadable
        .stream(offset) -> AsyncIterator[bytes]   raises MediaUnreadable (the tunnel ends that stream with END error)
        .aclose()
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable, Dict, Optional

import httpx

from ...domain.drive_index_params import TRANSCRIBE_MAX_BYTES
from ...domain.drive_media_source import MediaSource
from ...domain.drive_transcript import MediaUnreadable
from ...utils.logger import logger

EXPIRY_GRACE_S = 30.0               # the relay's RELAY_GRACE_S: the source must outlive the run's wall by this much
UPSTREAM_CONNECT_S = 10.0
UPSTREAM_READ_S = 30.0              # ffmpeg's own -rw_timeout in the old path
_CONTENT_RANGE = re.compile(r"^bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})$")


class HttpRangeSource:
    def __init__(self, source: MediaSource, *, wall_s: float, transport: Optional[httpx.AsyncBaseTransport] = None,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        if not source.url.startswith("https://"):
            logger.error("[SandboxMedia] the media source url is not https: refused")
            raise MediaUnreadable("media url is not https")
        current = (now or _utc_now)()
        if source.expires_at is not None:
            expires = source.expires_at if source.expires_at.tzinfo else source.expires_at.replace(tzinfo=timezone.utc)
            if expires < current + timedelta(seconds=wall_s + EXPIRY_GRACE_S):
                logger.warning("[SandboxMedia] the media source expires before the run ends: refused")
                raise MediaUnreadable("media source expires within the run")
        self._url = source.url
        self._headers: Dict[str, str] = dict(source.headers)
        self._size: Optional[int] = None
        # trust_env=False: no system proxy; http2=False: one plain connection per range; no redirect is ever followed.
        self._client = httpx.AsyncClient(
            http2=False, trust_env=False, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(UPSTREAM_READ_S, connect=UPSTREAM_CONNECT_S))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def size(self) -> int:
        """The recording's size in bytes, from one `bytes=0-0` range."""
        try:
            async with self._client.stream("GET", self._url, headers=self._request_headers(0, 0)) as resp:
                self._check(resp)
                match = _CONTENT_RANGE.fullmatch(resp.headers.get("Content-Range", ""))
                if match is None or int(match.group(1)) != 0 or int(match.group(2)) != 0:
                    raise MediaUnreadable("upstream size answer invalid")
                total = int(match.group(3))
        except httpx.HTTPError as exc:
            logger.warning("[SandboxMedia] upstream size request failed: %s", type(exc).__name__)
            raise MediaUnreadable(f"upstream {type(exc).__name__}") from None
        if not 1 <= total <= TRANSCRIBE_MAX_BYTES:
            logger.warning("[SandboxMedia] the recording's size is out of range: %d bytes", total)
            raise MediaUnreadable(f"source size {total} out of range")
        self._size = total
        return total

    async def stream(self, offset: int) -> AsyncIterator[bytes]:
        """The bytes from `offset` to the end, as the upstream sends them, never past the size `size()` read."""
        total = self._size
        if total is None or not 0 <= offset < total:
            raise MediaUnreadable("range outside the source")
        position = offset
        try:
            async with self._client.stream("GET", self._url, headers=self._request_headers(offset, None)) as resp:
                self._check(resp)
                match = _CONTENT_RANGE.fullmatch(resp.headers.get("Content-Range", ""))
                if match is None or int(match.group(1)) != offset or int(match.group(3)) != total:
                    raise MediaUnreadable("upstream Content-Range does not match")
                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    if position + len(chunk) > total:
                        raise MediaUnreadable("upstream sent past the source's end")
                    position += len(chunk)
                    yield chunk
        except httpx.HTTPError as exc:
            logger.warning("[SandboxMedia] upstream range ended after %d bytes: %s", position - offset,
                           type(exc).__name__)
            raise MediaUnreadable(f"upstream {type(exc).__name__}") from None

    def _request_headers(self, first: int, last: Optional[int]) -> Dict[str, str]:
        wanted = f"bytes={first}-{last}" if last is not None else f"bytes={first}-"
        return {**self._headers, "Accept-Encoding": "identity", "Range": wanted}

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code != 206:
            logger.warning("[SandboxMedia] upstream answered %d to a range request", resp.status_code)
            raise MediaUnreadable(f"upstream answered {resp.status_code}")
        encoding = resp.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            logger.warning("[SandboxMedia] upstream sent an encoded body")
            raise MediaUnreadable("upstream body encoded")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
