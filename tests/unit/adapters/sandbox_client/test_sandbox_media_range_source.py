"""HttpRangeSource at its HTTP boundary (httpx MockTransport): the upstream rules the loopback relay had (S5 design §2.3
"Size" and upstream rules), and no URL or token in any error."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import httpx
import pytest

from src.adapters.sandbox_client.media_range_source import EXPIRY_GRACE_S, HttpRangeSource
from src.domain.drive_index_params import TRANSCRIBE_MAX_BYTES
from src.domain.drive_media_source import MediaSource
from src.domain.drive_transcript import MediaUnreadable

URL = "https://www.example.com/drive/v3/files/rec-community-garden-0042?alt=media"
TOKEN = "tok-drive-garden-7Q2"
DATA = bytes(range(256)) * 400
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
SECRETS = (URL, TOKEN, "www.example.com", "alt=media", "rec-community-garden-0042")


class Chunks(httpx.AsyncByteStream):
    """A body that really streams (a `content=` body is pre-read by httpx and cannot be iterated raw)."""

    def __init__(self, data: bytes, size: int = 8192) -> None:
        self.data = data
        self.size = size

    async def __aiter__(self):
        for at in range(0, len(self.data), self.size):
            yield self.data[at:at + self.size]


def google(expires_at=None) -> MediaSource:
    return MediaSource(url=URL, headers={"Authorization": f"Bearer {TOKEN}"}, expires_at=expires_at)


class Upstream:
    """A Range-serving drive; `answer` may replace the response for a test."""

    def __init__(self, data: bytes = DATA, answer=None) -> None:
        self.data = data
        self.answer = answer
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answer is not None:
            return self.answer(request)
        first, _, last = request.headers["Range"].removeprefix("bytes=").partition("-")
        end = int(last) if last else len(self.data) - 1
        body = self.data[int(first):end + 1]
        return httpx.Response(206, stream=Chunks(body), headers={
            "Content-Range": f"bytes {first}-{end}/{len(self.data)}", "Content-Length": str(len(body))})


def source_over(upstream: Upstream, source: MediaSource = None, wall_s: float = 240.0) -> HttpRangeSource:
    return HttpRangeSource(source or google(), wall_s=wall_s, transport=httpx.MockTransport(upstream),
                           now=lambda: NOW)


async def collect(source: HttpRangeSource, offset: int) -> bytes:
    return b"".join([chunk async for chunk in source.stream(offset)])


def assert_clean(error: BaseException) -> None:
    text = f"{error} {error!r}"
    assert not any(secret in text for secret in SECRETS)
    assert error.__cause__ is None


class TestReads:
    async def test_size_comes_from_one_zero_byte_range_with_the_drives_own_headers(self):
        upstream = Upstream()
        source = source_over(upstream)
        assert await source.size() == len(DATA)
        request = upstream.requests[0]
        assert request.headers["Range"] == "bytes=0-0"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"     # the drive gets its token; nobody else does
        assert str(request.url) == URL

    async def test_a_stream_is_one_open_ended_range_cut_at_the_size(self):
        upstream = Upstream()
        source = source_over(upstream)
        await source.size()
        assert await collect(source, 40_000) == DATA[40_000:]
        assert upstream.requests[1].headers["Range"] == "bytes=40000-"

    async def test_the_bytes_are_never_decoded(self):
        upstream = Upstream(answer=lambda r: httpx.Response(206, content=b"x", headers={
            "Content-Range": f"bytes 0-0/{len(DATA)}", "Content-Encoding": "gzip"}))
        with pytest.raises(MediaUnreadable) as err:
            await source_over(upstream).size()
        assert_clean(err.value)


class TestRefusals:
    def test_only_https(self):
        with pytest.raises(MediaUnreadable) as err:
            HttpRangeSource(MediaSource(url="http://www.example.com/r"), wall_s=60, now=lambda: NOW)
        assert_clean(err.value)

    @pytest.mark.parametrize("expires_in", [0, 240 + EXPIRY_GRACE_S - 1])
    def test_a_source_that_expires_within_the_run_is_refused(self, expires_in):
        with pytest.raises(MediaUnreadable) as err:
            HttpRangeSource(google(NOW + timedelta(seconds=expires_in)), wall_s=240, now=lambda: NOW)
        assert_clean(err.value)

    def test_a_naive_expiry_is_read_as_utc(self):
        with pytest.raises(MediaUnreadable):
            HttpRangeSource(google(datetime(2026, 11, 4, 12, 1)), wall_s=240, now=lambda: NOW)
        HttpRangeSource(google(datetime(2026, 11, 4, 12, 30)), wall_s=240, now=lambda: NOW)

    async def test_a_redirect_is_not_followed(self):
        upstream = Upstream(answer=lambda r: httpx.Response(302, headers={"Location": "https://other.example.com/x"}))
        with pytest.raises(MediaUnreadable) as err:
            await source_over(upstream).size()
        assert len(upstream.requests) == 1
        assert_clean(err.value)

    @pytest.mark.parametrize("answer", [
        lambda r: httpx.Response(200, content=DATA),                                         # a range ignored
        lambda r: httpx.Response(206, content=b"x", headers={"Content-Range": "bytes 0-0/*"}),
        lambda r: httpx.Response(206, content=b"x", headers={"Content-Range": "bytes 1-1/9"}),
        lambda r: httpx.Response(403, content=b"denied"),
    ], ids=["200", "unknown-total", "wrong-start", "403"])
    async def test_a_size_answer_outside_the_rules_is_unreadable(self, answer):
        with pytest.raises(MediaUnreadable) as err:
            await source_over(Upstream(answer=answer)).size()
        assert_clean(err.value)

    async def test_a_size_over_the_transcription_cap_is_refused_before_any_sandbox_call(self):
        answer = lambda r: httpx.Response(206, content=b"x", headers={  # noqa: E731
            "Content-Range": f"bytes 0-0/{TRANSCRIBE_MAX_BYTES + 1}"})
        with pytest.raises(MediaUnreadable):
            await source_over(Upstream(answer=answer)).size()

    @pytest.mark.parametrize("content_range", [
        "bytes 10-{last}/{total}", "bytes 0-{last}/{other}", None,
    ], ids=["start-differs", "total-differs", "missing"])
    async def test_a_stream_whose_content_range_does_not_match_is_unreadable(self, content_range):
        upstream = Upstream()
        source = source_over(upstream)
        await source.size()

        def answer(request: httpx.Request) -> httpx.Response:
            headers = {}
            if content_range is not None:
                headers["Content-Range"] = content_range.format(last=len(DATA) - 1, total=len(DATA),
                                                                other=len(DATA) + 5)
            return httpx.Response(206, content=DATA, headers=headers)

        upstream.answer = answer
        with pytest.raises(MediaUnreadable) as err:
            await collect(source, 0)
        assert_clean(err.value)

    async def test_a_stream_that_runs_past_the_size_is_unreadable(self):
        upstream = Upstream()
        source = source_over(upstream)
        await source.size()
        upstream.answer = lambda r: httpx.Response(206, stream=Chunks(DATA + b"extra"), headers={
            "Content-Range": f"bytes 0-{len(DATA) - 1}/{len(DATA)}"})
        with pytest.raises(MediaUnreadable):
            await collect(source, 0)

    async def test_a_stream_before_the_size_or_outside_it_is_refused(self):
        source = source_over(Upstream())
        with pytest.raises(MediaUnreadable):
            await collect(source, 0)
        await source.size()
        with pytest.raises(MediaUnreadable):
            await collect(source, len(DATA))

    async def test_a_transport_failure_names_only_its_kind(self):
        def answer(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {URL}", request=request)

        with pytest.raises(MediaUnreadable) as err:
            await source_over(Upstream(answer=answer)).size()
        assert err.value.detail == "upstream ConnectError"
        assert_clean(err.value)
