"""Widget renderer: Chromium starts with the same network-closing flags as the sandbox render engine."""
import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.adapters.playwright_html_renderer import PlaywrightHtmlRenderer

NETWORK_FLAGS = {
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-server=127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--disable-background-networking",
}


async def _launch_args(monkeypatch, cloud_run: bool):
    seen = {}

    async def launch(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace()

    class _Starter:
        async def start(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    module = ModuleType("playwright.async_api")
    module.async_playwright = lambda: _Starter()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    renderer = PlaywrightHtmlRenderer()
    renderer._is_cloud_run = cloud_run
    await renderer._launch_browser()
    return seen["args"]


@pytest.mark.parametrize("cloud_run", [False, True])
async def test_the_network_flags_are_always_passed(monkeypatch, cloud_run):
    assert NETWORK_FLAGS <= set(await _launch_args(monkeypatch, cloud_run))


async def test_the_container_flags_stay_on_cloud_run_only(monkeypatch):
    container = {"--no-sandbox", "--disable-setuid-sandbox"}
    assert container <= set(await _launch_args(monkeypatch, True))
    assert not container & set(await _launch_args(monkeypatch, False))
