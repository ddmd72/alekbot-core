"""Home Screen install for the call page: manifest + icons, served without a session.

The main Quart app has no static folder, so these are explicit blueprint routes. iOS fetches the
manifest and apple-touch-icon without the session cookie, so they must not sit behind auth.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.web.voice_web_call_app import create_voice_web_call_blueprint


def _app():
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(side_effect=ValueError("no session"))
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        session_service=session_service, call_setup=MagicMock(), media_room=AsyncMock(),
        ephemeral_store=AsyncMock(), relay_base_url="wss://relay.example.com"))
    return app


@pytest.mark.asyncio
async def test_manifest_is_public_and_starts_on_the_call_page():
    resp = await _app().test_client().get("/cabinet/call/manifest.webmanifest")

    assert resp.status_code == 200
    assert resp.mimetype == "application/manifest+json"
    manifest = json.loads(await resp.get_data())
    assert manifest["start_url"] == "/cabinet/call"
    assert manifest["display"] == "standalone"


@pytest.mark.asyncio
async def test_every_manifest_icon_is_served():
    client = _app().test_client()
    manifest = json.loads(await (await client.get("/cabinet/call/manifest.webmanifest")).get_data())

    for icon in manifest["icons"]:
        resp = await client.get(icon["src"])
        assert resp.status_code == 200, icon["src"]
        assert resp.mimetype == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [180, 192, 512])
async def test_icon_sizes_are_served(size):
    resp = await _app().test_client().get(f"/cabinet/call/icon-{size}.png")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unknown_icon_size_is_404():
    resp = await _app().test_client().get("/cabinet/call/icon-64.png")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_call_page_links_the_manifest_and_touch_icon():
    body = (await (await _app().test_client().get("/cabinet/call")).get_data()).decode()

    assert '<link rel="manifest" href="/cabinet/call/manifest.webmanifest"' in body
    assert '<link rel="apple-touch-icon" href="/cabinet/call/icon-180.png"' in body
