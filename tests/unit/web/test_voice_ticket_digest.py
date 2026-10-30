"""No raw voice ticket in any store key or stored field (SECRETS_AT_REST_RFC §2.5, S4).

The real blueprints share one `FirestoreEphemeralStore` over the evaluating fake and a real AES-GCM test cipher.
"""
import json
import re
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from quart import Quart

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.domain.voice_ticket_keys import ticket_key
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb, eval_transactions

DIGEST_KEY = b"k" * 32
AUTH = {"Authorization": "Bearer tok"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.store = FirestoreEphemeralStore(self.db, collection="voice_tickets")
        self.cipher = AesGcmTestCipher()
        self.alerts = AsyncMock()
        agent = MagicMock()
        agent.session_config = AsyncMock(return_value={"instructions": "you are Lelik", "tools": []})
        agent.execute = AsyncMock()
        self.agent = agent
        provider = AsyncMock(return_value=agent)
        users = AsyncMock()
        users.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1"})
        self.media = AsyncMock()
        self.media.open_caller.return_value = MagicMock(session_id="S1", answer_sdp="ANSWER")
        self.media.attach_agent.return_value = MagicMock(adapter_ids=["A"], offer_sdp="OFFER2")

        async def oidc(_authorization):
            return True

        app = Quart(__name__)
        app.register_blueprint(create_voice_webhook_blueprint(
            user_repository=users, ephemeral_store=self.store, alert_sink=self.alerts,
            notification_service=AsyncMock(), lelik_agent_provider=provider,
            answer_url="https://main.example.test/voice/answer", signature_verifier=AsyncMock(return_value=True),
            relay_stream_url="wss://relay.example.test/stream", cipher=self.cipher, digest_key=DIGEST_KEY))
        app.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
            summary_consumer=AsyncMock(), oidc_verifier=oidc, alert_sink=self.alerts, digest_key=DIGEST_KEY))
        setup = VoiceCallSetupService(self.store, self.alerts, provider, digest_key=DIGEST_KEY)
        app.register_blueprint(create_voice_web_call_blueprint(
            session_service=sessions, call_setup=setup, media_room=self.media, ephemeral_store=self.store,
            relay_base_url="wss://relay.example.test", cipher=self.cipher, digest_key=DIGEST_KEY))
        self.client = app.test_client()

    @property
    def docs(self) -> dict:
        return self.db.collection("voice_tickets").docs

    def dump(self) -> str:
        return json.dumps(self.docs, default=str)


@pytest.fixture
def world():
    with eval_transactions():
        yield World()


async def _phone_setup(world: World) -> str:
    """Dial in, end the dial, and return the ticket the callback was originated with."""
    await world.client.post("/voice/auth", form={"From": "+15550100", "CallSid": "CA0001"})
    await world.client.post("/voice/inbound-status", form={"CallSid": "CA0001", "CallStatus": "completed"})
    return world.agent.execute.await_args.kwargs["ticket"]


async def _web_setup(world: World):
    resp = await world.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
    call_id = (await resp.get_json())["call_id"]
    await world.client.post(f"/api/voice/web-call/{call_id}/connect", headers=AUTH)
    url = world.media.attach_agent.await_args.args[1]
    return call_id, parse_qs(urlsplit(url).query)["ticket"][0]


@pytest.mark.asyncio
async def test_phone_call_stores_no_raw_ticket_and_hands_it_back_through_ciphertext(world):
    ticket = await _phone_setup(world)
    assert UUID.fullmatch(ticket)
    assert ticket not in world.dump()
    assert ticket_key(DIGEST_KEY, ticket) in world.docs


@pytest.mark.asyncio
async def test_phone_hand_out_record_holds_ciphertext_of_the_ticket(world):
    await world.client.post("/voice/auth", form={"From": "+15550100", "CallSid": "CA0001"})
    record = world.docs["voice_pending_callback:CA0001"]["value"]
    assert "ticket" not in record
    ticket = await world.cipher.decrypt(record["ticket_ct"], aad="voice/voice_pending_callback:CA0001")
    assert ticket_key(DIGEST_KEY, ticket) in world.docs
    assert ticket not in world.dump()


@pytest.mark.asyncio
async def test_phone_hand_out_ciphertext_does_not_decrypt_under_another_record(world):
    await world.client.post("/voice/auth", form={"From": "+15550100", "CallSid": "CA0001"})
    ct = world.docs["voice_pending_callback:CA0001"]["value"]["ticket_ct"]
    with pytest.raises(Exception):
        await world.cipher.decrypt(ct, aad="voice/voice_pending_callback:CA0002")


@pytest.mark.asyncio
async def test_phone_ticket_redeems_by_the_presented_value_exactly_once(world):
    ticket = await _phone_setup(world)
    # the persona was assembled on the answer webhook
    await world.client.post(f"/voice/answer?ticket={ticket}", form={})
    first = await world.client.post("/voice/session-config", json={"ticket": ticket})
    second = await world.client.post("/voice/session-config", json={"ticket": ticket})
    assert first.status_code == 200 and (await first.get_json())["call_kind"] == "phone"
    assert second.status_code == 404
    assert ticket not in world.dump()


@pytest.mark.asyncio
async def test_a_stored_key_is_not_a_redeemable_ticket(world):
    ticket = await _phone_setup(world)
    digest = ticket_key(DIGEST_KEY, ticket).split(":", 1)[1]
    resp = await world.client.post("/voice/session-config", json={"ticket": digest})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_web_call_stores_no_raw_ticket_and_hand_out_decrypts_to_the_url_ticket(world):
    call_id, ticket = await _web_setup(world)
    record = world.docs[f"voice_web_call:{call_id}"]["value"]
    assert "ticket" not in record
    assert await world.cipher.decrypt(record["ticket_ct"], aad=f"voice/voice_web_call:{call_id}") == ticket
    assert ticket not in world.dump()
    assert ticket_key(DIGEST_KEY, ticket) in world.docs


@pytest.mark.asyncio
async def test_web_hand_out_ciphertext_copied_under_another_record_fails(world):
    call_id, _ = await _web_setup(world)
    ct = world.docs[f"voice_web_call:{call_id}"]["value"]["ticket_ct"]
    with pytest.raises(Exception):
        await world.cipher.decrypt(ct, aad="voice/voice_web_call:other")


@pytest.mark.asyncio
async def test_web_ticket_redeems_by_the_presented_value_exactly_once(world):
    _, ticket = await _web_setup(world)
    first = await world.client.post("/voice/session-config", json={"ticket": ticket})
    second = await world.client.post("/voice/session-config", json={"ticket": ticket})
    assert first.status_code == 200 and (await first.get_json())["call_kind"] == "web"
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_amd_verdict_and_delegation_markers_are_keyed_by_digest(world):
    ticket = await _phone_setup(world)
    await world.client.post(f"/voice/status?ticket={ticket}", form={"AnsweredBy": "machine_start"})
    await world.client.post("/voice/delegate/abandon", json={"ticket": ticket, "call_id": "CA0001"})
    assert ticket not in world.dump()
    assert not any(ticket in key for key in world.docs)
    assert any(key.startswith("voice_amd:") for key in world.docs)
    assert any(key.startswith("voice_delegation_abandoned:") for key in world.docs)
