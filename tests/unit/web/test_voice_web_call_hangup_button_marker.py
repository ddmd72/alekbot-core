"""Only the hang-up button writes the marker that keeps a call from being resumed (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.domain.voice_ticket_keys import button_end_key
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher

AUTH = {"Authorization": "Bearer tok"}
KEY = b"k" * 32


class _Store:
    def __init__(self):
        self.data, self.ttls = {}, {}

    async def set(self, key, value, ttl_s):
        self.data[key], self.ttls[key] = value, ttl_s

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


async def _hung_up(body):
    store, cipher = _Store(), AesGcmTestCipher()
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "usr-1", "account_id": "acc-1"})
    agent = MagicMock()
    agent.session_config = AsyncMock(return_value={"instructions": "p", "tools": []})
    room = AsyncMock()
    room.open_caller.return_value = CallerLeg("S1", "ANSWER")
    room.attach_agent.return_value = AgentLeg(["A-in", "A-eg"], "OFFER2")
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        session_service=session_service,
        call_setup=VoiceCallSetupService(store, AsyncMock(), AsyncMock(return_value=agent), digest_key=KEY),
        media_room=room, ephemeral_store=store, relay_base_url="wss://relay.example.com", cipher=cipher,
        digest_key=KEY))
    client = app.test_client()
    started = await client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
    call_id = (await started.get_json())["call_id"]
    record = store.data[f"voice_web_call:{call_id}"]
    ticket = await cipher.decrypt(record["ticket_ct"], aad=f"voice/voice_web_call:{call_id}")
    kwargs = {"json": body} if body is not None else {}
    response = await client.post(f"/api/voice/web-call/{call_id}/hangup", headers=AUTH, **kwargs)
    assert response.status_code == 200
    return store, button_end_key(KEY, ticket)


@pytest.mark.asyncio
async def test_a_button_hangup_writes_the_marker():
    store, marker = await _hung_up({"trigger": "button"})

    assert marker in store.data
    assert store.ttls[marker] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"trigger": "status_ended"}, {"trigger": "superseded"}, {"trigger": "start_failed"},
                                  {"trigger": "nonsense"}, {}, None])
async def test_any_other_hangup_writes_no_marker(body):
    store, marker = await _hung_up(body)

    assert marker not in store.data
