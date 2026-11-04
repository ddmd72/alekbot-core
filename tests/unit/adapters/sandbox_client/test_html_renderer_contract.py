"""PlaywrightHtmlRenderer vs SandboxHtmlRenderer: equal observable PNGs and errors (card F; RFC §4.7).

Sandbox variant: client -> real sandbox_app (ASGI) -> real HtmlRenderEngine -> real node + headless shell.
The in-process variant needs Playwright Chromium; the sandbox variant needs node, Puppeteer and a headless shell.
The IHDR width of a PNG is 2 x the viewport width (device scale factor 2) in both.
"""
import asyncio
import os
import shutil
import struct
import sys

import pytest

from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer
from src.adapters.sandbox_client.sandbox_html_renderer import SandboxHtmlRenderer
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.html_render_engine import (
    BROWSER_MEMORY_MODE, BROWSER_NOFILE, BROWSER_NPROC, HtmlRenderEngine,
)
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.ports.html_renderer_port import HtmlRenderError
from tests.unit.adapters.sandbox_client.render_html_helpers import ROME_TRIP_HTML, render_app_transport
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, good_token
from tests.unit.web.test_sandbox_app import FakeEngine

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_CACHE_DIR = os.environ.get("PUPPETEER_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "puppeteer")
_PW_DIRS = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "",
            os.path.expanduser("~/Library/Caches/ms-playwright"), os.path.expanduser("~/.cache/ms-playwright")]


def _has_headless_shell() -> bool:
    shell_dir = os.path.join(_CACHE_DIR, "chrome-headless-shell")
    return os.path.isdir(shell_dir) and bool(os.listdir(shell_dir))


def _has_playwright_chromium() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return any(d and os.path.isdir(d) and any(n.startswith("chromium") for n in os.listdir(d)) for d in _PW_DIRS)


_SANDBOX_READY = (shutil.which("node") is not None
                  and os.path.isdir(os.path.join(_ROOT, "pdf_generator", "node_modules", "puppeteer"))
                  and _has_headless_shell()
                  and os.geteuid() != 0 and sys.platform in ("linux", "darwin"))

CAR_CARD_HTML = (
    "<div style=\"font-family:sans-serif;padding:16px;border-radius:12px;background:#e8eef6\">"
    "<h3>Car insurance</h3><p>Example Mutual Insurance, ends in 8204</p>"
    "<p>renews 2027-04-01</p><p><b>412.00 EUR</b></p></div>"
)
ROME_CARD = (
    "<div style=\"font-family:sans-serif;padding:16px;border-radius:12px;background:#f4efe6\">"
    "<h3>Albergo Immaginario</h3><p>Booking RM-55120</p><p>10–14 Aug 2025</p><p><b>560 EUR</b></p></div>"
)


async def _no_sleep(_s):
    return None


def _sandbox_renderer(tmp_path) -> SandboxHtmlRenderer:
    nproc = 4000 if sys.platform == "darwin" else BROWSER_NPROC
    launcher = ChildLauncher(memory_mode=BROWSER_MEMORY_MODE, nproc=nproc, nofile=BROWSER_NOFILE)
    out_root = tmp_path / "engine-out"
    out_root.mkdir(mode=0o700)
    engine = HtmlRenderEngine(launcher, op=SandboxOp.RENDER_PNG, output_root=str(out_root),
                              browser_cache_dir=_CACHE_DIR)
    transport = render_app_transport(tmp_path, png_engine=engine)
    return SandboxHtmlRenderer(SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep))


@pytest.fixture(params=[
    pytest.param("inprocess", marks=pytest.mark.skipif(not _has_playwright_chromium(), reason="needs Playwright Chromium")),
    pytest.param("sandbox", marks=pytest.mark.skipif(not _SANDBOX_READY, reason="needs node, puppeteer, headless shell, non-root")),
])
async def renderer(request, tmp_path):
    if request.param == "inprocess":
        r = PlaywrightHtmlRenderer()
        yield r
        await r.stop()
    else:
        yield _sandbox_renderer(tmp_path)


def _ihdr_width(png: bytes) -> int:
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    return struct.unpack(">II", png[16:24])[0]


@pytest.mark.slow
async def test_rome_card_at_480_is_960_wide(renderer):
    assert _ihdr_width(await renderer.render(ROME_CARD, 480)) == 960


@pytest.mark.slow
async def test_car_insurance_card_at_320_is_640_wide(renderer):
    assert _ihdr_width(await renderer.render(CAR_CARD_HTML, 320)) == 640


@pytest.mark.slow
async def test_rome_trip_page_fragment_renders_a_png_in_both(renderer):
    assert _ihdr_width(await renderer.render(ROME_TRIP_HTML, 480)) == 960


class _SleepingEngine(FakeEngine):
    """Sleeps past the request's wall budget, then reports what the real engine reports: `timeout`."""

    async def run(self, body_path, workdir, limits, meta):
        await asyncio.sleep(limits.wall_s + 0.1)
        return await super().run(body_path, workdir, limits, meta)


async def test_engine_sleeping_past_wall_s_is_a_timed_out_render_error(tmp_path):
    """Always-on: a stub engine behind the real app and client."""
    transport = render_app_transport(tmp_path, png_engine=_SleepingEngine(tmp_path, status=SandboxStatus.TIMEOUT))
    renderer = SandboxHtmlRenderer(SandboxHttpClient(BASE, good_token, transport=transport, sleep=_no_sleep),
                                   wall_s=0.5)
    with pytest.raises(HtmlRenderError) as info:
        await renderer.render(ROME_CARD, 480)
    assert "timed out" in str(info.value)
