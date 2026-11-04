"""SandboxPuppeteerRunner: wire tests at the HTTP boundary (RFC §4.7; card D)."""
import base64
import json

import httpx
import pytest

from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from src.ports.puppeteer_runner_port import PuppeteerRunnerError
from tests.unit.adapters.sandbox_client.render_html_helpers import HOUSEHOLD_BUDGET_HTML, render_app_transport
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, Scripted, good_token, reply

MEM, CPU, FILE = 1 << 29, 60.0, 1 << 20


async def _no_sleep(_s):
    return None


def runner_for(transport) -> SandboxPuppeteerRunner:
    client = SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep)
    return SandboxPuppeteerRunner(client, memory_bytes=MEM, cpu_s=CPU, file_bytes=FILE)


async def test_success_returns_pdf_bytes():
    assert await runner_for(Scripted(reply(200, SandboxStatus.OK, b"%PDF"))).run("<p>x</p>", 60) == b"%PDF"


async def test_request_shape_path_body_meta_keys_and_limits():
    transport = Scripted(reply(200, SandboxStatus.OK, b"%PDF"))
    await runner_for(transport).run(HOUSEHOLD_BUDGET_HTML, 45)
    request = transport.requests[0]
    assert request.url.path == "/v1/render.pdf"
    assert request.content == HOUSEHOLD_BUDGET_HTML.encode("utf-8")
    raw = json.loads(base64.urlsafe_b64decode(request.headers[sc.META_HEADER] + "=="))
    assert set(raw) == {"request_id", "limits"}
    limits = sc.decode_meta(request.headers[sc.META_HEADER]).limits
    assert (limits.wall_s, limits.memory_bytes, limits.cpu_s, limits.file_bytes) == (45.0, MEM, 45.0, FILE)


async def test_cpu_is_the_configured_cap_when_the_wall_is_longer():
    transport = Scripted(reply(200, SandboxStatus.OK, b"%PDF"))
    await runner_for(transport).run("<p>x</p>", 120)
    assert sc.decode_meta(transport.requests[0].headers[sc.META_HEADER]).limits.cpu_s == CPU


@pytest.mark.parametrize("code,status,detail,text", [
    (504, SandboxStatus.TIMEOUT, "", "Puppeteer process timed out after 60s"),
    (422, SandboxStatus.EMPTY_OUTPUT, "", "Puppeteer exited 0 but stdout is empty — no PDF bytes produced"),
    (422, SandboxStatus.SCRIPT_ERROR, "", "PDF render failed in the sandbox"),
    (413, SandboxStatus.TOO_LARGE, "", "PDF exceeded the size limit"),
    (400, SandboxStatus.BAD_REQUEST, "", "PDF render refused the request (bad_request)"),
    (500, SandboxStatus.ERROR, "sandbox_lost", "sandbox_lost: the run ended without a result twice"),
    (500, SandboxStatus.ERROR, "engine_crash", "sandbox error: engine_crash"),
    (500, SandboxStatus.ERROR, "", "sandbox error: error"),
])
async def test_every_status_maps_to_its_text(code, status, detail, text):
    with pytest.raises(PuppeteerRunnerError) as info:
        await runner_for(Scripted(reply(code, status, b"never shown", detail))).run("<p>x</p>", 60)
    assert str(info.value).startswith(text)
    assert "never shown" not in str(info.value)


async def test_misconfigured_401_becomes_a_runner_error_chained():
    with pytest.raises(PuppeteerRunnerError) as info:
        await runner_for(Scripted(reply(401))).run("<p>x</p>", 60)
    assert str(info.value) == "PDF rendering is unavailable (SandboxMisconfigured); try again later"
    assert isinstance(info.value.__cause__, SandboxMisconfigured)


async def test_recycling_twice_becomes_a_runner_error_wrapping_unavailable():
    busy = reply(503, SandboxStatus.ERROR, b"", "recycling")
    with pytest.raises(PuppeteerRunnerError) as info:
        await runner_for(Scripted(busy, busy)).run("<p>x</p>", 60)
    assert str(info.value) == "PDF rendering is unavailable (SandboxUnavailable); try again later"
    assert isinstance(info.value.__cause__, SandboxUnavailable)


async def test_reset_twice_is_sandbox_lost():
    transport = Scripted(httpx.ReadError("reset"))
    with pytest.raises(PuppeteerRunnerError, match="^sandbox_lost: "):
        await runner_for(transport).run("<p>x</p>", 60)
    assert len(transport.requests) == 2


async def test_real_app_round_trip_delivers_the_html_to_the_engine(tmp_path):
    transport = render_app_transport(tmp_path)
    pdf = await runner_for(transport).run(HOUSEHOLD_BUDGET_HTML, 60)
    assert pdf == b"%PDF-1.7"
    assert transport.pdf_engine.calls[0][3] == HOUSEHOLD_BUDGET_HTML.encode("utf-8")
    assert transport.png_engine.calls == []
