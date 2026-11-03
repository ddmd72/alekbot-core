"""SandboxHttpClient SP2: one retry on recycling and on a lost instance, none on busy, the deadline caps a retry, a
file body streams whole on every attempt, and the summary line's level follows the outcome."""
import logging
import time

import httpx
import pytest

from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import (
    BASE, LIMITS, Scripted, good_token, reply,
)

OP = SandboxOp.RENDER_DOCX


async def no_sleep(_s: float) -> None:
    return None


def client_for(transport) -> SandboxHttpClient:
    return SandboxHttpClient(BASE, good_token, transport=transport, sleep=no_sleep)


def _wall(request: httpx.Request) -> float:
    return sc.decode_meta(request.headers[sc.META_HEADER]).limits.wall_s


def _summaries(caplog):
    return [r for r in caplog.records if "[SandboxClient]" in r.getMessage() and "wall_s=" in r.getMessage()
            and "status=" in r.getMessage()]


async def test_recycling_once_then_ok_takes_two_requests():
    transport = Scripted(reply(503, SandboxStatus.RECYCLING), reply(200, SandboxStatus.OK, b"fine"))
    result = await client_for(transport).call(OP, b"b", LIMITS)
    assert result.outcome.status is SandboxStatus.OK and result.body == b"fine"
    assert len(transport.requests) == 2


async def test_recycling_twice_is_busy_after_exactly_two_requests():
    transport = Scripted(reply(503, SandboxStatus.RECYCLING))
    with pytest.raises(SandboxUnavailable) as caught:
        await client_for(transport).call(OP, b"b", LIMITS)
    assert caught.value.reason == "busy" and caught.value.charges_file is False
    assert len(transport.requests) == 2


@pytest.mark.parametrize("response", [reply(429), reply(503)])
async def test_429_and_a_plain_503_are_busy_without_a_retry(response):
    transport = Scripted(response)
    with pytest.raises(SandboxUnavailable) as caught:
        await client_for(transport).call(OP, b"b", LIMITS)
    assert caught.value.reason == "busy"
    assert len(transport.requests) == 1


async def test_connect_refused_is_unavailable_without_a_retry():
    transport = Scripted(httpx.ConnectError("refused"))
    with pytest.raises(SandboxUnavailable) as caught:
        await client_for(transport).call(OP, b"b", LIMITS)
    assert caught.value.reason == "unavailable"
    assert len(transport.requests) == 1


async def test_lost_twice_is_the_outcome_sandbox_lost_after_one_retry(caplog):
    transport = Scripted(httpx.ReadError("reset"))
    with caplog.at_level(logging.INFO):
        result = await client_for(transport).call(OP, b"b", LIMITS)
    assert (result.outcome.status, result.outcome.detail) == (SandboxStatus.ERROR, "sandbox_lost")
    assert len(transport.requests) == 2
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_no_retry_past_the_deadline_and_no_request():
    # first attempt runs, the deadline passes while it is answered, the retry never starts
    deadline = time.time() + 0.2

    def slow_then_recycling(request: httpx.Request):
        time.sleep(0.3)
        return reply(503, SandboxStatus.RECYCLING)

    transport = Scripted(slow_then_recycling)
    result = await client_for(transport).call(OP, b"b", LIMITS, deadline_at=deadline)
    assert (result.outcome.status, result.outcome.detail) == (SandboxStatus.TIMEOUT, "deadline")
    assert len(transport.requests) == 1


async def test_a_retry_runs_with_its_wall_lowered_to_the_time_left():
    transport = Scripted(reply(503, SandboxStatus.RECYCLING), reply(200, SandboxStatus.OK, b"x"))
    deadline = time.time() + 5.0
    await client_for(transport).call(OP, b"b", LIMITS, deadline_at=deadline)
    first, second = (_wall(r) for r in transport.requests)
    assert first == LIMITS.wall_s                 # the first attempt is the caller's own limits
    assert 0 < second <= 5.0 < LIMITS.wall_s


async def test_body_path_sends_the_exact_bytes_with_content_length_and_whole_again_on_a_retry(tmp_path):
    payload = bytes(range(256)) * 9000            # 2.3 MB: more than one 1 MiB chunk
    path = tmp_path / "input.pdf"
    path.write_bytes(payload)
    seen = []

    def record(request: httpx.Request):
        seen.append((request.headers.get("Content-Length"), request.content))
        return reply(503, SandboxStatus.RECYCLING) if len(seen) == 1 else reply(200, SandboxStatus.OK, b"ok")

    result = await client_for(Scripted(record)).call(OP, None, LIMITS, body_path=str(path))
    assert result.outcome.status is SandboxStatus.OK
    assert seen == [(str(len(payload)), payload), (str(len(payload)), payload)]


async def test_exactly_one_of_body_and_body_path():
    client = client_for(Scripted(reply(200, SandboxStatus.OK)))
    with pytest.raises(ValueError):
        await client.call(OP, None, LIMITS)
    with pytest.raises(ValueError):
        await client.call(OP, b"b", LIMITS, body_path="/nonexistent")


async def test_summary_line_is_info_on_ok(caplog):
    with caplog.at_level(logging.INFO):
        await client_for(Scripted(reply(200, SandboxStatus.OK, b"x"))).call(OP, b"b", LIMITS)
    assert [r.levelno for r in _summaries(caplog)] == [logging.INFO]


@pytest.mark.parametrize("response", [reply(500, SandboxStatus.ERROR, b"boom"), reply(429)])
async def test_summary_line_is_a_warning_on_failure_and_on_unavailable(response, caplog):
    with caplog.at_level(logging.INFO):
        try:
            await client_for(Scripted(response)).call(OP, b"b", LIMITS)
        except SandboxUnavailable:
            pass
    assert [r.levelno for r in _summaries(caplog)] == [logging.WARNING]


async def test_summary_line_is_an_error_on_misconfigured(caplog):
    with caplog.at_level(logging.INFO):
        with pytest.raises(SandboxMisconfigured):
            await client_for(Scripted(reply(403))).call(OP, b"b", LIMITS)
    assert logging.ERROR in [r.levelno for r in _summaries(caplog)]
