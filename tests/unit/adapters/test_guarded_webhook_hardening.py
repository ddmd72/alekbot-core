"""GuardedWebhookAdapter, hardening round (EXTERNAL_TASK_QUEUE_RFC §5.8): the answer is asked for and counted as raw
bytes (a compressed body cannot expand past the cap), and the log filter's flag ends with the send, so an httpx record
the caller logs afterwards in the same task is kept. The worker is woken at `hooks.example.com`.
"""
import gzip
import logging
from typing import List

import httpx

from src.adapters.outbound_webhook import guarded_webhook_adapter as guarded
from src.adapters.outbound_webhook.guarded_webhook_adapter import GuardedWebhookAdapter
from src.domain.wake_refusal import WakeRefusal
from src.domain.wake_target import WakeTarget

PUBLIC = "93.184.216.34"
TARGET = WakeTarget("https://hooks.example.com/wake")
BODY = {"event": "tasks_available", "worker": "wrk-1"}


async def _resolve(host: str) -> List[str]:
    return [PUBLIC]


def _adapter(handler, *, resolve=_resolve, read_max: int = 1_000) -> GuardedWebhookAdapter:
    return GuardedWebhookAdapter({"svc.example.org"}, resolve=resolve, transport=httpx.MockTransport(handler),
                                 read_max=read_max)


async def test_the_request_asks_for_an_uncompressed_answer():
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)
    assert await _adapter(handler).post(TARGET, BODY) is None
    assert seen[0].headers.get_list("accept-encoding") == ["identity"]


async def test_a_user_accept_encoding_header_is_refused_before_sending():
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)
    target = WakeTarget("https://hooks.example.com/wake", (("Accept-Encoding", "gzip"),))
    assert await _adapter(handler).post(target, BODY) is WakeRefusal.HEADER
    assert seen == []


async def test_the_cap_counts_raw_bytes_so_a_compressed_body_is_not_expanded():
    """A gzip body of a few KiB that expands to 10 MiB, sent with `Content-Encoding: gzip`. The read stops after the
    raw bytes reach the cap; the expanded text is never produced."""
    bomb = gzip.compress(b"\0" * 10_000_000)
    pulled: List[int] = []

    async def stream():
        for start in range(0, len(bomb), 100):
            pulled.append(start)
            yield bomb[start:start + 100]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=stream())

    decoded: List[int] = []
    original = httpx.Response.aiter_bytes

    async def counting(self, *args, **kwargs):      # would see the expanded text if the adapter decoded it
        async for chunk in original(self, *args, **kwargs):
            decoded.append(len(chunk))
            yield chunk
    httpx.Response.aiter_bytes = counting
    try:
        assert await _adapter(handler, read_max=1_000).post(TARGET, BODY) is None
    finally:
        httpx.Response.aiter_bytes = original
    assert len(pulled) <= 10 and decoded == []


async def test_a_body_over_the_cap_stops_the_read_and_the_status_still_decides():
    pulled: List[int] = []

    async def stream():
        for i in range(500):
            pulled.append(i)
            yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=stream())
    assert await _adapter(handler, read_max=1_000).post(TARGET, BODY) is WakeRefusal.CLIENT_ERROR
    assert len(pulled) <= 11


async def test_the_log_flag_ends_with_the_send_and_a_later_httpx_record_is_kept(caplog):
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.INFO)
    try:
        with caplog.at_level(logging.INFO):
            assert await _adapter(lambda request: httpx.Response(204)).post(TARGET, BODY) is None
            assert guarded._SENDING.get() is False
            httpx_logger.info("caller record after a delivered send")

            async def no_dns(host: str) -> List[str]:
                raise OSError("no such host")
            assert await _adapter(lambda r: httpx.Response(204), resolve=no_dns).post(TARGET, BODY) is \
                WakeRefusal.DNS_FAILURE
            assert guarded._SENDING.get() is False
            httpx_logger.info("caller record after a failed send")

            def broken(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("refused")
            assert await _adapter(broken).post(TARGET, BODY) is WakeRefusal.UNREACHABLE
            assert guarded._SENDING.get() is False
            httpx_logger.info("caller record after an unreachable send")
    finally:
        httpx_logger.setLevel(logging.WARNING)
    messages = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert messages == ["caller record after a delivered send", "caller record after a failed send",
                        "caller record after an unreachable send"]
