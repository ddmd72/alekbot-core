"""Web-call hangup vs connect (final-review finding 3): hangup deletes the call record, and a
connect whose record disappears while the relay is being attached closes the adapters it just
created and answers 404 instead of writing the record back."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.services.voice_call_setup_service import VoiceCallSetupService
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


def _app(store, media_room):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1"})
    agent = MagicMock()
    agent.session_config = AsyncMock(return_value={"instructions": "you are Lelik", "tools": []})
    setup = VoiceCallSetupService(store, AsyncMock(), AsyncMock(return_value=agent))
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        session_service=session_service, call_setup=setup, media_room=media_room,
        ephemeral_store=store, relay_base_url="wss://relay.example.com"))
    return app


def _room():
    room = AsyncMock()
    room.open_caller.return_value = CallerLeg("S1", "ANSWER")
    room.attach_agent.return_value = AgentLeg(["A-in", "A-eg"], "OFFER2")
    return room


async def _start(client):
    resp = await client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
    assert resp.status_code == 201
    return (await resp.get_json())["call_id"]


@pytest.mark.asyncio
async def test_hangup_deletes_the_call_record():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    call_id = await _start(client)
    await client.post(f"/api/voice/web-call/{call_id}/connect", headers=AUTH)
    assert f"voice_web_call:{call_id}" in store.data

    resp = await client.post(f"/api/voice/web-call/{call_id}/hangup", headers=AUTH)
    assert resp.status_code == 200
    assert f"voice_web_call:{call_id}" not in store.data
    room.close.assert_awaited_once_with(["A-in", "A-eg"])
    # Idempotent from the page's point of view: a second hangup finds nothing and closes nothing.
    again = await client.post(f"/api/voice/web-call/{call_id}/hangup", headers=AUTH)
    assert again.status_code == 404
    assert room.close.await_count == 1


@pytest.mark.asyncio
async def test_connect_whose_record_vanishes_during_attach_closes_its_adapters_and_404s():
    store, room = MemoryStore(), _room()
    client = _app(store, room).test_client()
    call_id = await _start(client)

    async def attach_while_hung_up(*_args):
        # The page's hangup lands while the SFU adapters are being created.
        store.data.pop(f"voice_web_call:{call_id}", None)
        return AgentLeg(["A-in", "A-eg"], "OFFER2")

    room.attach_agent.side_effect = attach_while_hung_up
    resp = await client.post(f"/api/voice/web-call/{call_id}/connect", headers=AUTH)

    assert resp.status_code == 404
    room.close.assert_awaited_once_with(["A-in", "A-eg"])
    assert f"voice_web_call:{call_id}" not in store.data
