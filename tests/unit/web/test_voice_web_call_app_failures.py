"""Web-call start(): body validation and non-MediaRoomError SFU failures both release cleanly.

Fix round 1, Finding 3 / Ruling 5: only `MediaRoomError` was caught around `open_caller`, so an
httpx timeout, a `KeyError` from a malformed body, or a store failure on the record write escaped
as a bare 500 while still holding the one-call marker and the ticket for their full TTLs. This
file covers the fix: body validation runs before `claim` (so a bad request never claims anything),
and any exception in the open_caller/record-write step releases the marker+ticket and returns 502.

Also covers Finding 2 / Ruling 4: `start()` no longer extends the one-call marker past the setup
TTL — that happens only when the relay redeems the ticket (see
test_voice_session_config_extends_marker.py). The MemoryStore here records the `ttl_s` each `set`
call used so that can be asserted directly.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.web.voice_web_call_app import create_voice_web_call_blueprint

AUTH = {"Authorization": "Bearer tok"}


class MemoryStore:
    """Like the brief's MemoryStore, but `set` also records the ttl_s it was called with, so
    tests can assert the marker was written at the short setup TTL, not the full call TTL."""

    def __init__(self):
        self.data = {}
        self.set_ttls = {}

    async def set(self, key, value, ttl_s):
        self.data[key] = value
        self.set_ttls[key] = ttl_s

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


async def _start(client, json_body=None):
    resp = await client.post("/api/voice/web-call", json=json_body if json_body is not None
                              else {"sdp": "OFFER", "mid": "0"}, headers=AUTH)
    return resp, await resp.get_json()


@pytest.mark.asyncio
async def test_start_with_a_plain_exception_from_open_caller_releases_and_502s():
    store, room = MemoryStore(), _room()
    room.open_caller.side_effect = RuntimeError("timeout")
    resp, _ = await _start(_app(store, room).test_client())
    assert resp.status_code == 502
    assert not any(k.startswith(("voice_one_call:", "voice_ticket:")) for k in store.data)


@pytest.mark.asyncio
async def test_start_with_missing_sdp_is_400_and_writes_no_marker():
    store, room = MemoryStore(), _room()
    resp, body = await _start(_app(store, room).test_client(), json_body={"mid": "0"})
    assert resp.status_code == 400
    assert body == {"error": "invalid request"}
    assert store.data == {}
    room.open_caller.assert_not_called()


@pytest.mark.asyncio
async def test_start_with_empty_sdp_is_400():
    store, room = MemoryStore(), _room()
    resp, _ = await _start(_app(store, room).test_client(), json_body={"sdp": "", "mid": "0"})
    assert resp.status_code == 400
    assert store.data == {}


@pytest.mark.asyncio
async def test_start_with_missing_mid_is_400():
    store, room = MemoryStore(), _room()
    resp, _ = await _start(_app(store, room).test_client(), json_body={"sdp": "OFFER"})
    assert resp.status_code == 400
    assert store.data == {}


@pytest.mark.asyncio
async def test_start_leaves_the_marker_at_the_setup_ttl_not_the_call_ttl():
    store, room = MemoryStore(), _room()
    resp, _ = await _start(_app(store, room).test_client())
    assert resp.status_code == 201
    # 300s == VoiceCallSetupService's default ticket_ttl_s, the setup window. It must NOT be
    # 3600 (the full call_ttl_s) — that extension now happens only in session_config when the
    # relay redeems the ticket, not here.
    assert store.set_ttls["voice_one_call:u1"] == 300
