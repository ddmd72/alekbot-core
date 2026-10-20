"""SandboxHttpClient (RFC §4.7, §4.9, §4.14): wire tests against the real sandbox app and a scripted transport."""
import logging

import httpx
import pytest

from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import (
    BASE, LIMITS, Scripted, good_token, outcome_header, real_app_transport, reply,
)

OP = SandboxOp.RENDER_DOCX


async def no_sleep(_s: float) -> None:
    return None


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
    assert set(request.headers) & {"x-user-id", "x-account-id", "cookie"} == set()
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
    with pytest.raises(SandboxUnavailable):
        await client_for(Scripted(reply(429))).call(OP, b"b", LIMITS)


@pytest.mark.parametrize("status", [None, SandboxStatus.UNCONFIGURED, SandboxStatus.ERROR])
async def test_503_other_is_unavailable(status):
    with pytest.raises(SandboxUnavailable):
        await client_for(Scripted(reply(503, status))).call(OP, b"b", LIMITS)


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
    assert lines[0].startswith(f"[SandboxClient] request_id={request_id} op=render.docx status=ok wall_s=")
    assert "SECRET" not in lines[0] and "good" not in lines[0] and BASE not in lines[0]
