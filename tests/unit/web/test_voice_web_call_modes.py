"""Voice modes on the Cabinet call: the start body, the mode pages, their manifests."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.voice_mode import UnsupportedVoiceMode, VoiceMode
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.voice_tickets import call_kind_key, ticket_key
from tests.unit.web.test_voice_web_call_app import AUTH, CIPHER, MemoryStore, _room


class RecordingAgent:
    def __init__(self, error=None):
        self.modes, self.error = [], error

    async def session_config(self, user_id, account_id, mode=VoiceMode.LELIK):
        self.modes.append(mode)
        if self.error:
            raise self.error
        return {"instructions": "x", "tools": []}


def _app(agent=None):
    store, agent = MemoryStore(), agent or RecordingAgent()
    setup = VoiceCallSetupService(store, AsyncMock(), AsyncMock(return_value=agent), digest_key=b"k" * 32)
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1"})
    app = Quart(__name__)
    room = _room()
    app.register_blueprint(create_voice_web_call_blueprint(
        session_service=session_service, call_setup=setup, media_room=room, ephemeral_store=store,
        relay_base_url="wss://relay.example.com", cipher=CIPHER, digest_key=b"k" * 32))
    return app, store, agent, room


async def _start(app, **extra):
    return await app.test_client().post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0", **extra}, headers=AUTH)


@pytest.mark.asyncio
async def test_missing_mode_is_lelik():
    app, store, agent, _ = _app()
    resp = await _start(app)
    assert resp.status_code == 201
    assert agent.modes == [VoiceMode.LELIK]
    assert [v["mode"] for k, v in store.data.items() if k.startswith("voice_call_kind:")] == ["lelik"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interpreter", "tutor"])
async def test_mode_reaches_prepare(mode):
    app, store, agent, _ = _app()
    resp = await _start(app, mode=mode)
    assert resp.status_code == 201
    assert agent.modes == [VoiceMode(mode)]
    assert [v["mode"] for k, v in store.data.items() if k.startswith("voice_ticket:")] == [mode]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["karaoke", "", 3, None])
async def test_unknown_mode_is_400_and_claims_nothing(mode):
    app, store, agent, room = _app()
    resp = await _start(app, mode=mode)
    assert resp.status_code == 400
    assert (await resp.get_json()) == {"error": "unknown_mode"}
    assert store.data == {} and agent.modes == []
    room.open_caller.assert_not_called()


@pytest.mark.asyncio
async def test_unavailable_mode_is_409_and_the_claim_is_released():
    app, store, _, room = _app(RecordingAgent(error=UnsupportedVoiceMode("no")))
    resp = await _start(app, mode="tutor")
    assert resp.status_code == 409
    assert (await resp.get_json()) == {"error": "mode_unavailable"}
    assert store.data == {}
    room.open_caller.assert_not_called()
    # Released: a retry meets the mode refusal again, not "a call is already in progress".
    retry = await _start(app, mode="tutor")
    assert (await retry.get_json()) == {"error": "mode_unavailable"}


MAIN_MANIFEST = '<link rel="manifest" href="/cabinet/call/manifest.webmanifest"'


@pytest.mark.asyncio
async def test_main_call_page_is_unchanged():
    body = (await (await _app()[0].test_client().get("/cabinet/call")).get_data()).decode()
    assert MAIN_MANIFEST in body
    assert "VOICE_MODE = " not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,title", [("interpreter", "Lelik Translator"), ("tutor", "Lelik Tutor")])
async def test_mode_page_and_manifest(mode, title):
    client = _app()[0].test_client()
    page = await client.get(f"/cabinet/call/{mode}")
    body = (await page.get_data()).decode()
    assert page.status_code == 200
    assert f'window.VOICE_MODE = "{mode}"' in body
    assert f'<link rel="manifest" href="/cabinet/call/{mode}/manifest.webmanifest"' in body
    assert MAIN_MANIFEST not in body

    resp = await client.get(f"/cabinet/call/{mode}/manifest.webmanifest")
    manifest = json.loads(await resp.get_data())
    base = json.load(open("src/web/static/call.webmanifest"))
    assert resp.mimetype == "application/manifest+json"
    assert manifest["start_url"] == f"/cabinet/call/{mode}"
    assert manifest["name"] == title
    assert manifest["short_name"] == title
    # Reviewer edit (per-mode Home Screen icons): the icons keep the base sizes and types, each src moved under the
    # mode's prefix; every other field still equals the base manifest.
    expected_icons = [dict(i, src=i["src"].replace("/cabinet/call/", f"/cabinet/call/{mode}/", 1)) for i in base["icons"]]
    assert [i["src"] for i in expected_icons] == [f"/cabinet/call/{mode}/icon-192.png", f"/cabinet/call/{mode}/icon-512.png"]
    assert manifest["icons"] == expected_icons
    assert {k: v for k, v in manifest.items() if k not in ("name", "short_name", "start_url", "icons")} == \
        {k: v for k, v in base.items() if k not in ("name", "short_name", "start_url", "icons")}


@pytest.mark.asyncio
async def test_modes_page_and_manifest():
    client = _app()[0].test_client()
    page = await client.get("/cabinet/call/modes")
    body = (await page.get_data()).decode()
    assert page.status_code == 200
    assert 'href="/cabinet/call/interpreter"' in body and 'href="/cabinet/call/tutor"' in body
    assert "/cabinet/call/modes/manifest.webmanifest" in body
    manifest = json.loads(await (await client.get("/cabinet/call/modes/manifest.webmanifest")).get_data())
    assert manifest["start_url"] == "/cabinet/call/modes"


@pytest.mark.asyncio
async def test_unknown_mode_page_is_404_and_icons_and_manifest_still_resolve():
    client = _app()[0].test_client()
    assert (await client.get("/cabinet/call/foo")).status_code == 404
    assert (await client.get("/cabinet/call/icon-192.png")).status_code == 200
    assert (await client.get("/cabinet/call/manifest.webmanifest")).status_code == 200
