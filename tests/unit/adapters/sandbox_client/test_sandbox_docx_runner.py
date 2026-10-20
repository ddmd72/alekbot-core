"""SandboxDocxRunner: framing and the mapping of outcomes to DocxRunnerError / propagation (RFC §4.9, plan G10)."""
import pytest

from src.adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxStatus
from src.domain.sandbox_docx_frame import split_body
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from src.ports.docx_runner_port import DocxRunnerError
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, Scripted, good_token, reply


async def _no_sleep(_s):
    return None


def runner_for(transport) -> SandboxDocxRunner:
    return SandboxDocxRunner(SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep))


async def test_success_returns_bytes():
    assert await runner_for(Scripted(reply(200, SandboxStatus.OK, b"PKdocx"))).run("js", "{}", 60) == b"PKdocx"


async def test_frame_layout():
    transport = Scripted(reply(200, SandboxStatus.OK, b"PK"))
    await runner_for(transport).run("console.log(1)", '{"t": "é"}', 45)
    request = transport.requests[0]
    assert request.content[:4] == len("console.log(1)".encode()).to_bytes(4, "big")
    assert split_body(request.content) == (b"console.log(1)", '{"t": "é"}'.encode())
    meta = sc.decode_meta(request.headers[sc.META_HEADER])
    assert meta.limits.wall_s == 45.0 and request.url.path == "/v1/render.docx"


@pytest.mark.parametrize("code,status,text", [
    (422, SandboxStatus.SCRIPT_ERROR, "exit code 1\nReferenceError: x is not defined"),
    (504, SandboxStatus.TIMEOUT, "Node.js process timed out after 60s"),
    (422, SandboxStatus.EMPTY_OUTPUT, "Node.js exited 0 but stdout is empty — no DOCX bytes produced"),
    (413, SandboxStatus.TOO_LARGE, "output exceeded the file size limit"),
])
async def test_script_error_text_reaches_docx_runner_error(code, status, text):
    with pytest.raises(DocxRunnerError) as info:
        await runner_for(Scripted(reply(code, status, text.encode()))).run("js", "{}", 60)
    assert str(info.value) == text


async def test_too_large_from_the_http_413_is_a_runner_error():
    import httpx
    with pytest.raises(DocxRunnerError) as info:
        await runner_for(Scripted(httpx.Response(413, content=b"big"))).run("js", "{}", 60)
    assert str(info.value) == "output exceeded the file size limit"


async def test_sandbox_lost_is_a_docx_runner_error_charged_to_the_script():
    with pytest.raises(DocxRunnerError, match="^sandbox_lost: the run ended without a result twice"):
        await runner_for(Scripted(reply(502))).run("js", "{}", 60)


async def test_bad_request_is_charged_to_the_script_not_systemic():
    with pytest.raises(DocxRunnerError) as info:
        await runner_for(Scripted(reply(400, SandboxStatus.BAD_REQUEST, b"", "x"))).run("js", "{}", 60)
    assert str(info.value) == "exit code 4\n(no stderr)"


@pytest.mark.parametrize("code,exc", [(503, SandboxUnavailable), (429, SandboxUnavailable),
                                      (401, SandboxMisconfigured), (403, SandboxMisconfigured)])
async def test_unavailable_and_misconfigured_propagate_unchanged(code, exc):
    with pytest.raises(exc) as info:
        await runner_for(Scripted(reply(code))).run("js", "{}", 60)
    assert not isinstance(info.value, DocxRunnerError)   # the repair loop must not see a systemic failure


@pytest.mark.parametrize("response", [
    lambda: reply(404, SandboxStatus.ERROR, b"Not Found", "http_404"),
    lambda: reply(400, SandboxStatus.BAD_REQUEST, b"bad meta", "bad_meta"),
    lambda: reply(404, SandboxStatus.BAD_REQUEST, b"unknown op", "unknown_op"),
])
async def test_wrong_path_and_server_refusals_are_not_charged_to_the_script(response):
    with pytest.raises(SandboxMisconfigured) as info:
        await runner_for(Scripted(response())).run("js", "{}", 60)
    assert not isinstance(info.value, DocxRunnerError)
