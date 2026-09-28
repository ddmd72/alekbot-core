"""Cabinet web-call API: auth, ownership, busy, failure releases, hangup before the relay starts."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.ports.media_room_port import MediaRoomError
from src.services.voice_call_setup_service import VoiceCallSetupError
from src.web.voice_web_call_app import create_voice_web_call_blueprint

AUTH = {"Authorization": "Bearer tok"}


class MemoryStore:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


def _setup(store, prepare_error=None):
    from src.services.voice_call_setup_service import VoiceCallSetupService
    agent = MagicMock()
    agent.session_config = AsyncMock(side_effect=prepare_error or None,
                                     return_value={"instructions": "you are Lelik", "tools": []})
    return VoiceCallSetupService(store, AsyncMock(), AsyncMock(return_value=agent))


def _app(store, media_room, user="u1", setup=None):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": user, "account_id": "a1"})
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        session_service=session_service, call_setup=setup or _setup(store), media_room=media_room,
        ephemeral_store=store, relay_base_url="wss://relay.example.com"))
    return app


def _room():
    room = AsyncMock()
    room.open_caller.return_value = CallerLeg("S1", "ANSWER")
    room.attach_agent.return_value = AgentLeg(["A-in", "A-eg"], "OFFER2")
    return room


async def _start(client):
    resp = await client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
    return resp, await resp.get_json()


@pytest.mark.asyncio
async def test_start_answers_and_never_returns_the_ticket():
    store, room = MemoryStore(), _room()
    resp, body = await _start(_app(store, room).test_client())
    assert resp.status_code == 201 and body["sdp"] == "ANSWER"
    record = store.data[f"voice_web_call:{body['call_id']}"]
    assert record["ticket"] not in str(body)
    assert store.data["voice_one_call:u1"]["call_id"] == body["call_id"]
    assert store.data[f"voice_ticket:{record['ticket']}"]["call_kind"] == "web"


@pytest.mark.asyncio
async def test_second_call_while_live_is_409_and_leaves_the_first_intact():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    _, first = await _start(client)
    resp, _ = await _start(client)
    assert resp.status_code == 409
    assert store.data["voice_one_call:u1"]["call_id"] == first["call_id"]


@pytest.mark.asyncio
async def test_connect_points_the_sfu_at_the_relay_with_the_ticket():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    _, body = await _start(client)
    ticket = store.data[f"voice_web_call:{body['call_id']}"]["ticket"]
    resp = await client.post(f"/api/voice/web-call/{body['call_id']}/connect", headers=AUTH)
    assert (await resp.get_json())["sdp"] == "OFFER2"
    room.attach_agent.assert_awaited_once_with(
        "S1", f"wss://relay.example.com/sfu/ingest?ticket={ticket}", f"wss://relay.example.com/sfu/egress?ticket={ticket}")


@pytest.mark.asyncio
async def test_sfu_failure_on_start_releases_marker_and_ticket():
    store, room = MemoryStore(), _room()
    room.open_caller.side_effect = MediaRoomError("down")
    resp, _ = await _start(_app(store, room).test_client())
    assert resp.status_code == 502
    assert not any(k.startswith(("voice_one_call:", "voice_ticket:")) for k in store.data)


@pytest.mark.asyncio
async def test_persona_failure_on_start_is_503_and_releases():
    store, room = MemoryStore(), _room()
    resp, _ = await _start(_app(store, room, setup=_setup(store, prepare_error=RuntimeError("no prompt"))).test_client())
    assert resp.status_code == 503
    assert "voice_one_call:u1" not in store.data
    room.open_caller.assert_not_called()


@pytest.mark.asyncio
async def test_hangup_before_relay_start_releases_and_allows_a_new_call():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    _, body = await _start(client)
    await client.post(f"/api/voice/web-call/{body['call_id']}/hangup", headers=AUTH)
    assert "voice_one_call:u1" not in store.data
    resp, _ = await _start(client)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_hangup_closes_the_adapters():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    _, body = await _start(client)
    await client.post(f"/api/voice/web-call/{body['call_id']}/connect", headers=AUTH)
    await client.post(f"/api/voice/web-call/{body['call_id']}/hangup", headers=AUTH)
    room.close.assert_awaited_once_with(["A-in", "A-eg"])


@pytest.mark.asyncio
async def test_status_is_live_only_while_the_marker_names_this_call():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    _, body = await _start(client)
    url = f"/api/voice/web-call/{body['call_id']}/status"
    assert (await (await client.get(url, headers=AUTH)).get_json())["state"] == "live"
    store.data["voice_one_call:u1"] = {"in_flight": True, "call_id": "someone-else"}
    assert (await (await client.get(url, headers=AUTH)).get_json())["state"] == "ended"


@pytest.mark.asyncio
async def test_another_user_cannot_touch_the_call():
    store, room = MemoryStore(), _room()
    _, body = await _start(_app(store, room).test_client())
    intruder = _app(store, room, user="u2").test_client()
    for path in ("connect", "hangup", "renegotiate"):
        resp = await intruder.post(f"/api/voice/web-call/{body['call_id']}/{path}", json={"sdp": "x"}, headers=AUTH)
        assert resp.status_code == 404
    assert (await intruder.get(f"/api/voice/web-call/{body['call_id']}/status", headers=AUTH)).status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_is_401():
    store, room = MemoryStore(), _room()
    session_less = Quart(__name__)
    ss = MagicMock()
    ss.verify_access_token = MagicMock(side_effect=ValueError("bad"))
    session_less.register_blueprint(create_voice_web_call_blueprint(
        session_service=ss, call_setup=_setup(store), media_room=room, ephemeral_store=store,
        relay_base_url="wss://relay.example.com"))
    resp = await session_less.test_client().post("/api/voice/web-call", json={"sdp": "o", "mid": "0"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_call_page_is_served_no_cache():
    resp = await _app(MemoryStore(), _room()).test_client().get("/cabinet/call")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-cache"
