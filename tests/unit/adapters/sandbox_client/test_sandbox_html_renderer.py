"""SandboxHtmlRenderer: wire tests at the HTTP boundary (RFC §4.7; card D)."""
import httpx
import pytest

from src.adapters.sandbox_client.sandbox_html_renderer import PNG_WALL_S, SandboxHtmlRenderer
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxStatus
from src.domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from src.domain.sandbox_png_frame import frame_png_body
from src.ports.html_renderer_port import HtmlRenderError
from tests.unit.adapters.sandbox_client.render_html_helpers import ROME_TRIP_HTML, render_app_transport
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, Scripted, good_token, reply

MEM, CPU, FILE = 1 << 29, 60.0, 1 << 20


async def _no_sleep(_s):
    return None


def renderer_for(transport, **kw) -> SandboxHtmlRenderer:
    client = SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep)
    return SandboxHtmlRenderer(client, memory_bytes=MEM, cpu_s=CPU, file_bytes=FILE, **kw)


async def test_success_returns_png_bytes():
    assert await renderer_for(Scripted(reply(200, SandboxStatus.OK, b"\x89PNG"))).render("<p>x</p>") == b"\x89PNG"


async def test_request_shape_path_framed_body_meta_and_wall():
    transport = Scripted(reply(200, SandboxStatus.OK, b"\x89PNG"))
    await renderer_for(transport).render(ROME_TRIP_HTML)
    request = transport.requests[0]
    assert request.url.path == "/v1/render.png"
    assert request.content == frame_png_body(480, ROME_TRIP_HTML.encode("utf-8"))
    meta = sc.decode_meta(request.headers[sc.META_HEADER])
    assert PNG_WALL_S == 30.0
    assert (meta.limits.wall_s, meta.limits.memory_bytes, meta.limits.cpu_s, meta.limits.file_bytes) == (
        30.0, MEM, CPU if CPU < 30.0 else 30.0, FILE)
    assert meta.kind is None and meta.ext is None


async def test_width_and_wall_are_passed_through():
    transport = Scripted(reply(200, SandboxStatus.OK, b"\x89PNG"))
    await renderer_for(transport, wall_s=12.0).render("<p>x</p>", width=640)
    request = transport.requests[0]
    assert request.content[:4] == (640).to_bytes(4, "big")
    assert sc.decode_meta(request.headers[sc.META_HEADER]).limits.wall_s == 12.0


async def test_width_out_of_range_is_a_render_error_without_a_call():
    transport = Scripted(reply(200, SandboxStatus.OK, b"\x89PNG"))
    with pytest.raises(HtmlRenderError):
        await renderer_for(transport).render("<p>x</p>", width=5)
    assert transport.requests == []


@pytest.mark.parametrize("code,status,detail,text", [
    (504, SandboxStatus.TIMEOUT, "", "PNG render timed out after 30s"),
    (422, SandboxStatus.EMPTY_OUTPUT, "", "PNG render produced no output"),
    (422, SandboxStatus.SCRIPT_ERROR, "", "PNG render failed in the sandbox"),
    (413, SandboxStatus.TOO_LARGE, "", "PNG exceeded the size limit"),
    (400, SandboxStatus.BAD_REQUEST, "", "PNG render refused the request (bad_request)"),
    (500, SandboxStatus.ERROR, "sandbox_lost", "sandbox_lost: the run ended without a result twice"),
    (500, SandboxStatus.ERROR, "engine_crash", "sandbox error: engine_crash"),
])
async def test_every_status_maps_to_its_text(code, status, detail, text):
    with pytest.raises(HtmlRenderError) as info:
        await renderer_for(Scripted(reply(code, status, b"never shown", detail))).render("<p>x</p>")
    assert str(info.value).startswith(text)
    assert "never shown" not in str(info.value)


async def test_misconfigured_401_becomes_a_render_error_chained():
    with pytest.raises(HtmlRenderError) as info:
        await renderer_for(Scripted(reply(401))).render("<p>x</p>")
    assert str(info.value) == "PNG rendering is unavailable (SandboxMisconfigured); try again later"
    assert isinstance(info.value.__cause__, SandboxMisconfigured)


async def test_recycling_twice_becomes_a_render_error_wrapping_unavailable():
    busy = reply(503, SandboxStatus.ERROR, b"", "recycling")
    with pytest.raises(HtmlRenderError) as info:
        await renderer_for(Scripted(busy, busy)).render("<p>x</p>")
    assert isinstance(info.value.__cause__, SandboxUnavailable)


async def test_reset_twice_is_sandbox_lost():
    with pytest.raises(HtmlRenderError, match="^sandbox_lost: "):
        await renderer_for(Scripted(httpx.ReadError("reset"))).render("<p>x</p>")


async def test_start_is_a_noop_and_stop_closes_the_transport():
    closed = []

    class Transport(Scripted):
        async def aclose(self) -> None:
            closed.append(True)

    renderer = renderer_for(Transport(reply(200, SandboxStatus.OK, b"\x89PNG")))
    await renderer.start()
    assert closed == []
    await renderer.stop()
    assert closed == [True]


async def test_real_app_round_trip_delivers_width_and_html_to_the_engine(tmp_path):
    transport = render_app_transport(tmp_path)
    png = await renderer_for(transport).render(ROME_TRIP_HTML)
    assert png == b"\x89PNG"
    assert transport.png_engine.calls[0][3] == frame_png_body(480, ROME_TRIP_HTML.encode("utf-8"))
    assert transport.pdf_engine.calls == []
