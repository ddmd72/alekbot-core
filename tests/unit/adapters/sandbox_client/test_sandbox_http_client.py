"""SandboxHttpClient (RFC §4.7, §4.9, §4.14): wire tests against the real sandbox app and a scripted transport."""
import asyncio
import gzip
import logging
import re

import httpx
import pytest

from src.adapters.sandbox_client import sandbox_http_client as http_client_module
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxOp, SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import (
    BASE, LIMITS, Scripted, good_token, outcome_header, real_app_transport, reply,
)

OP = SandboxOp.RENDER_DOCX


async def no_sleep(_s: float) -> None:
    return None


class _RawStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        yield self._data


class _Trickle(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(200):
            await asyncio.sleep(0.05)
            yield b"x"


def client_for(transport, **kw) -> SandboxHttpClient:
    return SandboxHttpClient(BASE, good_token, transport=transport, sleep=no_sleep, **kw)


# --- wire tests against the REAL sandbox_app ---------------------------------------------------------------

async def test_request_carries_bearer_and_meta_and_no_identity(tmp_path):
    transport = real_app_transport(tmp_path)
    seen = []
    original = transport.handle_async_request

    async def spy(request):
        seen.append(request)
        return await original(request)

    transport.handle_async_request = spy
    reply_ = await client_for(transport).call(OP, b"payload", LIMITS)
    assert reply_.outcome.status == SandboxStatus.OK and reply_.body == b"DOCXBYTES"
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer good"
    assert request.url.path == "/v1/render.docx"
    allowed = {"host", "authorization", "content-type", "content-length", "accept-encoding", "accept",
               "user-agent", "connection", sc.META_HEADER.lower()}
    assert set(request.headers) <= allowed, set(request.headers) - allowed
    assert transport.engine.calls[0][3] == b"payload"


async def test_meta_field_set_on_the_wire():
    transport = Scripted(reply(200, SandboxStatus.OK, b"x"))
    await client_for(transport).call(OP, b"b", LIMITS, kind="docx", ext="docx")
    meta = sc._b64_decode(transport.requests[0].headers[sc.META_HEADER], sc.META_MAX_BYTES)
    assert set(meta) == {"request_id", "limits", "kind", "ext"}
    assert set(meta["limits"]) == {"wall_s", "memory_bytes", "cpu_s", "file_bytes"}


async def test_request_id_is_fresh_per_attempt():
    transport = Scripted(reply(503, SandboxStatus.RECYCLING), reply(200, SandboxStatus.OK, b"x"))
    await client_for(transport).call(OP, b"b", LIMITS)
    ids = [sc.decode_meta(r.headers[sc.META_HEADER]).request_id for r in transport.requests]
    assert len(set(ids)) == 2


async def test_outcome_header_validated_strictly():
    bad = httpx.Response(200, headers={sc.OUTCOME_HEADER: "not-base64-json!"}, content=b"x")
    out = (await client_for(Scripted(bad)).call(OP, b"b", LIMITS)).outcome
    assert out.status == SandboxStatus.ERROR
    # a closed-schema violation (extra key) is malformed too
    extra = sc._b64_encode({"status": "ok", "detail": "", "stats": {}, "user": "u"})
    bad2 = httpx.Response(200, headers={sc.OUTCOME_HEADER: extra}, content=b"x")
    assert (await client_for(Scripted(bad2)).call(OP, b"b", LIMITS)).outcome.status == SandboxStatus.ERROR


async def test_body_is_not_parsed():
    payload = b'{"status": "script_error"}\x00\xff PK'
    out = await client_for(Scripted(reply(200, SandboxStatus.OK, payload))).call(OP, b"b", LIMITS)
    assert out.outcome.status == SandboxStatus.OK and out.body == payload


async def test_timeout_is_wall_plus_15():
    transport = Scripted(reply(200, SandboxStatus.OK, b"x"))
    await client_for(transport).call(OP, b"b", LIMITS)
    timeout = transport.requests[0].extensions["timeout"]
    assert timeout["read"] == LIMITS.wall_s + 15 == timeout["write"] == timeout["pool"]


async def test_client_disables_env_proxies_and_enables_http2():
    client = SandboxHttpClient(BASE, good_token)
    try:
        assert client._client._trust_env is False  # noqa: SLF001 - pinning the S0.1 finding
        assert client._client._transport._pool._http2 is True  # noqa: SLF001
    finally:
        await client.aclose()


async def test_base_url_normalised_without_trailing_slash():
    transport = Scripted(reply(200, SandboxStatus.OK, b"x"))
    await SandboxHttpClient(BASE + "/", good_token, transport=transport).call(OP, b"b", LIMITS)
    assert str(transport.requests[0].url) == BASE + "/v1/render.docx"


# --- failure mapping, one test per row of §4.9 -------------------------------------------------------------

async def test_recycling_retried_up_to_three_times_uncharged():
    transport = Scripted(reply(503, SandboxStatus.RECYCLING), reply(503, SandboxStatus.RECYCLING),
                         reply(200, SandboxStatus.OK, b"ok"))
    out = await client_for(transport).call(OP, b"b", LIMITS)
    assert out.outcome.status == SandboxStatus.OK and len(transport.requests) == 3


async def test_recycling_three_times_then_unavailable():
    transport = Scripted(reply(503, SandboxStatus.RECYCLING))
    with pytest.raises(SandboxUnavailable):
        await client_for(transport).call(OP, b"b", LIMITS)
    assert len(transport.requests) == 3


async def test_429_is_unavailable():
    transport = Scripted(reply(429))
    with pytest.raises(SandboxUnavailable):
        await client_for(transport).call(OP, b"b", LIMITS)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status", [None, SandboxStatus.ERROR])
async def test_503_other_is_unavailable(status):
    transport = Scripted(reply(503, status))
    with pytest.raises(SandboxUnavailable):
        await client_for(transport).call(OP, b"b", LIMITS)
    assert len(transport.requests) == 1


async def test_503_unconfigured_is_unavailable_without_retry():
    transport = Scripted(reply(503, SandboxStatus.UNCONFIGURED))
    with pytest.raises(SandboxUnavailable):
        await client_for(transport).call(OP, b"b", LIMITS)
    assert len(transport.requests) == 1


async def test_connection_refused_is_unavailable():
    with pytest.raises(SandboxUnavailable):
        await client_for(Scripted(httpx.ConnectError("refused"))).call(OP, b"b", LIMITS)


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("status", [None, SandboxStatus.ERROR, SandboxStatus.OK])
async def test_401_and_403_are_misconfigured_and_log_error(code, status, caplog):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SandboxMisconfigured):
            await client_for(Scripted(reply(code, status))).call(OP, b"b", LIMITS)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.parametrize("failure", [httpx.ReadError("reset"), httpx.RemoteProtocolError("reset"), reply(502)])
async def test_reset_retried_once_then_sandbox_lost(failure):
    transport = Scripted(failure)
    out = (await client_for(transport).call(OP, b"b", LIMITS)).outcome
    assert (out.status, out.detail) == (SandboxStatus.ERROR, "sandbox_lost")
    assert len(transport.requests) == 2


async def test_reset_then_success_is_success():
    transport = Scripted(httpx.ReadError("reset"), reply(200, SandboxStatus.OK, b"ok"))
    assert (await client_for(transport).call(OP, b"b", LIMITS)).outcome.status == SandboxStatus.OK


async def test_timeout_retried_once_then_sandbox_lost():
    transport = Scripted(httpx.ReadTimeout("slow"))
    out = (await client_for(transport).call(OP, b"b", LIMITS)).outcome
    assert (out.status, out.detail) == (SandboxStatus.ERROR, "sandbox_lost")
    assert len(transport.requests) == 2


async def test_500_with_error_outcome_is_an_engine_verdict_not_retried():
    transport = Scripted(reply(500, SandboxStatus.ERROR, b"engine failure"))
    out = await client_for(transport).call(OP, b"b", LIMITS)
    assert out.outcome.status == SandboxStatus.ERROR and out.outcome.detail == ""
    assert len(transport.requests) == 1


async def test_504_with_timeout_outcome_is_decided_by_the_header():
    transport = Scripted(reply(504, SandboxStatus.TIMEOUT, b"Node.js process timed out after 60s"))
    out = await client_for(transport).call(OP, b"b", LIMITS)
    assert out.outcome.status == SandboxStatus.TIMEOUT and len(transport.requests) == 1


async def test_malformed_envelope_is_error():
    out = (await client_for(Scripted(httpx.Response(200, content=b"x"))).call(OP, b"b", LIMITS)).outcome
    assert (out.status, out.detail) == (SandboxStatus.ERROR, "malformed_envelope")


async def test_413_is_too_large():
    transport = Scripted(httpx.Response(413, content=b"big"))
    assert (await client_for(transport).call(OP, b"b", LIMITS)).outcome.status == SandboxStatus.TOO_LARGE
    assert len(transport.requests) == 1


async def test_token_provider_failure_is_misconfigured():
    async def broken() -> str:
        raise RuntimeError("no metadata server")

    client = SandboxHttpClient(BASE, broken, transport=Scripted(reply(200, SandboxStatus.OK)))
    with pytest.raises(SandboxMisconfigured):
        await client.call(OP, b"b", LIMITS)


async def test_failure_text_is_capped():
    big = b"x" * (sc.ERROR_TEXT_CAP_BYTES * 3)
    out = await client_for(Scripted(reply(422, SandboxStatus.SCRIPT_ERROR, big))).call(OP, b"b", LIMITS)
    assert len(out.body) <= sc.ERROR_TEXT_CAP_BYTES


# --- log ---------------------------------------------------------------------------------------------------

async def test_client_logs_request_id_op_status_wall_only(caplog):
    transport = Scripted(reply(200, SandboxStatus.OK, b"SECRET-PAYLOAD"))
    with caplog.at_level(logging.INFO):
        await client_for(transport).call(OP, b"SECRET-BODY", LIMITS)
    lines = [r.getMessage() for r in caplog.records if "[SandboxClient]" in r.getMessage()]
    assert len(lines) == 1
    request_id = sc.decode_meta(transport.requests[0].headers[sc.META_HEADER]).request_id
    assert re.fullmatch(rf"\[SandboxClient\] request_id={request_id} op=render\.docx status=ok wall_s=\d+\.\d{{3}}",
                        lines[0]), lines[0]


async def test_log_lists_every_attempt_request_id(caplog):
    transport = Scripted(reply(503, SandboxStatus.RECYCLING), reply(200, SandboxStatus.OK, b"x"))
    with caplog.at_level(logging.INFO):
        await client_for(transport).call(OP, b"b", LIMITS)
    ids = [sc.decode_meta(r.headers[sc.META_HEADER]).request_id for r in transport.requests]
    line = [r.getMessage() for r in caplog.records if "[SandboxClient]" in r.getMessage()][0]
    assert f"request_id={','.join(ids)} " in line


# --- response hardening ------------------------------------------------------------------------------------

def _gzip(data: bytes) -> bytes:
    return gzip.compress(data)


@pytest.mark.parametrize("encoding,layers", [("gzip", 1), ("gzip, gzip", 2)])
async def test_content_encoded_reply_is_malformed_and_never_decoded(encoding, layers):
    body = b"x" * 100_000
    for _ in range(layers):
        body = _gzip(body)
    # Raw bytes behind a Content-Encoding: httpx would decode them on aiter_bytes; the client must not.
    transport = Scripted(httpx.Response(200, headers={sc.OUTCOME_HEADER: outcome_header(SandboxStatus.OK),
                                                      "Content-Encoding": encoding},
                                        stream=_RawStream(body)))
    out = await client_for(transport).call(OP, b"b", LIMITS)
    assert (out.outcome.status, out.outcome.detail, out.body) == (SandboxStatus.ERROR, "malformed_envelope", b"")


async def test_request_asks_for_identity_encoding():
    transport = Scripted(reply(200, SandboxStatus.OK, b"x"))
    await client_for(transport).call(OP, b"b", LIMITS)
    assert transport.requests[0].headers["Accept-Encoding"] == "identity"


async def test_ok_body_over_the_response_cap_is_too_large_not_truncated(monkeypatch):
    monkeypatch.setattr(http_client_module, "RESPONSE_CAP_BYTES", 10)
    out = await client_for(Scripted(reply(200, SandboxStatus.OK, b"x" * 11))).call(OP, b"b", LIMITS)
    assert (out.outcome.status, out.outcome.detail, out.body) == (SandboxStatus.TOO_LARGE, "response_too_large", b"")
    ok = await client_for(Scripted(reply(200, SandboxStatus.OK, b"x" * 10))).call(OP, b"b", LIMITS)
    assert ok.outcome.status == SandboxStatus.OK and len(ok.body) == 10


async def test_trickling_server_hits_the_total_deadline_and_is_lost(monkeypatch):
    monkeypatch.setattr(http_client_module, "TIMEOUT_MARGIN_S", 0.1)
    limits = SandboxLimits(wall_s=0.1, memory_bytes=1 << 30, cpu_s=1.0, file_bytes=1 << 20)
    transport = Scripted(httpx.Response(200, headers={sc.OUTCOME_HEADER: outcome_header(SandboxStatus.OK)},
                                        stream=_Trickle()))
    out = (await client_for(transport).call(OP, b"b", limits)).outcome
    assert (out.status, out.detail) == (SandboxStatus.ERROR, "sandbox_lost")
    assert len(transport.requests) == 2


@pytest.mark.parametrize("code", [404, 405])
async def test_wrong_url_without_an_outcome_is_misconfigured(code, caplog):
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SandboxMisconfigured):
            await client_for(Scripted(httpx.Response(code, content=b"<html>not found</html>"))).call(OP, b"b", LIMITS)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_unknown_op_with_an_outcome_stays_an_outcome():
    out = await client_for(Scripted(reply(404, SandboxStatus.BAD_REQUEST, b"unknown op"))).call(OP, b"b", LIMITS)
    assert out.outcome.status == SandboxStatus.BAD_REQUEST


async def test_headerless_413_is_too_large_without_the_front_end_html():
    out = await client_for(Scripted(httpx.Response(413, content=b"<html>Request Entity Too Large</html>"))).call(
        OP, b"b", LIMITS)
    assert out.outcome.status == SandboxStatus.TOO_LARGE and out.body == b""


@pytest.mark.parametrize("error,expected", [
    (httpx.DecodingError("bad"), SandboxUnavailable),
    (httpx.LocalProtocolError("bad header"), SandboxUnavailable),
    (httpx.UnsupportedProtocol("no scheme"), SandboxMisconfigured),
])
async def test_unclassified_httpx_errors_never_escape_raw(error, expected, caplog):
    with caplog.at_level(logging.INFO):
        with pytest.raises(expected):
            await client_for(Scripted(error)).call(OP, b"b", LIMITS)
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SandboxClient] request_id=")][0]
    assert "status=unavailable" in line or "status=misconfigured" in line


async def test_provider_classified_errors_pass_through_unchanged():
    async def unavailable() -> str:
        raise SandboxUnavailable("metadata blip")

    client = SandboxHttpClient(BASE, unavailable, transport=Scripted(reply(200, SandboxStatus.OK)))
    with pytest.raises(SandboxUnavailable):
        await client.call(OP, b"b", LIMITS)
