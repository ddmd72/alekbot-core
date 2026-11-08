"""Per-mode Home Screen icons: the mode page's apple-touch-icon, the mode manifest's icons and the icon route."""
import json
import os

import pytest

from tests.unit.web.test_voice_web_call_modes import _app

MODES = ["interpreter", "tutor"]
SIZES = (180, 192, 512)
STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "web", "static")
CALL_180 = "/cabinet/call/icon-180.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_mode_page_links_its_own_touch_icon(mode):
    app = _app()[0]
    resp = await app.test_client().get(f"/cabinet/call/{mode}")
    html = (await resp.get_data()).decode()
    assert resp.status_code == 200
    assert f'href="/cabinet/call/{mode}/icon-180.png"' in html
    assert CALL_180 not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_mode_manifest_lists_the_mode_icons(mode):
    app = _app()[0]
    resp = await app.test_client().get(f"/cabinet/call/{mode}/manifest.webmanifest")
    assert json.loads(await resp.get_data())["icons"] == [
        {"src": f"/cabinet/call/{mode}/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": f"/cabinet/call/{mode}/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("size", SIZES)
async def test_mode_icon_route_serves_the_static_file_without_auth(mode, size):
    app = _app()[0]
    resp = await app.test_client().get(f"/cabinet/call/{mode}/icon-{size}.png")  # no Authorization header
    with open(os.path.join(STATIC, f"{mode}-icon-{size}.png"), "rb") as f:
        expected = f.read()
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert await resp.get_data() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_mode_icon_outside_the_allowlist_is_404(mode):
    app = _app()[0]
    resp = await app.test_client().get(f"/cabinet/call/{mode}/icon-64.png")
    assert resp.status_code == 404
    assert json.loads(await resp.get_data()) == {"error": "not found"}


@pytest.mark.asyncio
async def test_main_call_page_keeps_the_call_icon():
    app = _app()[0]
    html = (await (await app.test_client().get("/cabinet/call")).get_data()).decode()
    assert f'href="{CALL_180}"' in html


@pytest.mark.asyncio
async def test_modes_manifest_keeps_the_call_icons():
    app = _app()[0]
    resp = await app.test_client().get("/cabinet/call/modes/manifest.webmanifest")
    assert [i["src"] for i in json.loads(await resp.get_data())["icons"]] == [
        "/cabinet/call/icon-192.png", "/cabinet/call/icon-512.png"]
