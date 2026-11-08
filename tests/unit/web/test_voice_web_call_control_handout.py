"""The control token's mint and hand-out on the main service (VOICE_CALL_CONTROL_CHANNEL_RFC §4.1, §9): minted at
start, only its digest on the ticket record, the token sealed on the call record, and `/connect` returning
`control: {url, token}` only while `VOICE_CONTROL_CHANNEL` is on."""
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.voice_tickets import DIGEST_KEY, open_ticket, ticket_key
from tests.unit.web.test_voice_web_call_app import AUTH, MemoryStore

RELAY = "wss://relay.example.test"


class World:
    def __init__(self):
        self.store = MemoryStore()
        self.cipher = AesGcmTestCipher()
        self.room = AsyncMock()
        self.room.open_caller.return_value = CallerLeg("S1", "ANSWER")
        self.room.attach_agent.return_value = AgentLeg(["A-in", "A-eg"], "OFFER2")
        agent = MagicMock()
        agent.session_config = AsyncMock(return_value={"instructions": "you are Lelik", "tools": []})
        session_service = MagicMock()
        session_service.verify_access_token = MagicMock(return_value={"sub": "usr-1", "account_id": "acc-1"})
        app = Quart(__name__)
        app.register_blueprint(create_voice_web_call_blueprint(
            session_service=session_service,
            call_setup=VoiceCallSetupService(self.store, AsyncMock(), AsyncMock(return_value=agent),
                                             digest_key=DIGEST_KEY),
            media_room=self.room, ephemeral_store=self.store, relay_base_url=RELAY, cipher=self.cipher,
            digest_key=DIGEST_KEY))
        self.client = app.test_client()

    async def start(self, **extra):
        resp = await self.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0", **extra}, headers=AUTH)
        assert resp.status_code == 201
        return (await resp.get_json())["call_id"]

    async def connect(self, call_id):
        resp = await self.client.post(f"/api/voice/web-call/{call_id}/connect", headers=AUTH)
        assert resp.status_code == 200
        return await resp.get_json()

    async def ticket_of(self, call_id):
        key = f"voice_web_call:{call_id}"
        return await open_ticket(self.cipher, key, self.store.data[key])


@pytest.mark.asyncio
@pytest.mark.parametrize("knob", [None, "off", "", "yes"])
async def test_knob_off_connect_carries_no_control(monkeypatch, knob):
    if knob is None:
        monkeypatch.delenv("VOICE_CONTROL_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("VOICE_CONTROL_CHANNEL", knob)
    w = World()
    body = await w.connect(await w.start(mode="interpreter"))
    assert body == {"sdp": "OFFER2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("knob", ["on", " ON "])
async def test_knob_on_connect_hands_out_the_relay_url_and_the_token(monkeypatch, knob):
    monkeypatch.setenv("VOICE_CONTROL_CHANNEL", knob)
    w = World()
    call_id = await w.start(mode="interpreter")
    body = await w.connect(call_id)

    assert set(body) == {"sdp", "control"} and body["sdp"] == "OFFER2"
    assert set(body["control"]) == {"url", "token"}
    token = body["control"]["token"]
    # same relay base as the SFU URLs: the page's socket reaches the process that runs the call
    ingest_url, egress_url = w.room.attach_agent.await_args.args[1:3]
    assert body["control"]["url"] == f"{RELAY}/ctl"
    assert ingest_url.startswith(f"{RELAY}/sfu/") and egress_url.startswith(f"{RELAY}/sfu/")
    # 32 random bytes, urlsafe; never in the URL
    assert len(token) >= 43 and token not in body["control"]["url"]
    # the relay gets only the digest, with the session config it already fetches
    ticket = await w.ticket_of(call_id)
    assert w.store.data[ticket_key(ticket)]["control_token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    # nothing stores the raw token in the clear, and the page still never sees the ticket
    assert token not in repr(w.store.data)
    assert ticket not in repr(body)


@pytest.mark.asyncio
async def test_the_token_is_per_call_and_stable_across_connects(monkeypatch):
    monkeypatch.setenv("VOICE_CONTROL_CHANNEL", "on")
    w = World()
    call_id = await w.start()
    first = (await w.connect(call_id))["control"]["token"]
    assert (await w.connect(call_id))["control"]["token"] == first
    await w.client.post(f"/api/voice/web-call/{call_id}/hangup", headers=AUTH, json={"trigger": "button"})
    other = (await w.connect(await w.start()))["control"]["token"]
    assert other != first


@pytest.mark.asyncio
async def test_the_token_is_minted_with_the_knob_off_so_turning_it_on_acts_at_connect(monkeypatch):
    monkeypatch.delenv("VOICE_CONTROL_CHANNEL", raising=False)
    w = World()
    call_id = await w.start()
    assert "control_token_sha256" in w.store.data[ticket_key(await w.ticket_of(call_id))]
    monkeypatch.setenv("VOICE_CONTROL_CHANNEL", "on")
    assert "control" in await w.connect(call_id)


@pytest.mark.asyncio
async def test_a_call_record_from_before_the_token_gets_no_control(monkeypatch):
    monkeypatch.setenv("VOICE_CONTROL_CHANNEL", "on")
    w = World()
    call_id = await w.start()
    key = f"voice_web_call:{call_id}"
    w.store.data[key] = {k: v for k, v in w.store.data[key].items() if k != "control_ct"}
    assert await w.connect(call_id) == {"sdp": "OFFER2"}


@pytest.mark.asyncio
async def test_the_sealed_token_is_bound_to_its_own_call_and_purpose(monkeypatch):
    from cryptography.exceptions import InvalidTag
    w = World()
    call_id = await w.start()
    record = w.store.data[f"voice_web_call:{call_id}"]
    with pytest.raises(InvalidTag):     # the ticket's AAD does not open the token
        await w.cipher.decrypt(record["control_ct"], aad=f"voice/voice_web_call:{call_id}")
