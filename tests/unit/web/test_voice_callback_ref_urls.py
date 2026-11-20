"""The Twilio callback URLs carry an encrypted reference, never the raw ticket (Cloud Run logs request URLs)."""
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from quart import Quart

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.agents.lelik_agent import LelikAgent
from src.domain.voice_callback_ref import to_callback_ref
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_ticket_keys import ticket_key
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.support.voice_callback import callback_ref_for, ticket_of

DIGEST_KEY = b"k" * 32


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.store = FirestoreEphemeralStore(self.db, collection="voice_tickets")
        self.cipher = AesGcmTestCipher()
        self.telephony = AsyncMock()
        self.telephony.originate_call.return_value = "CA0002"
        agent = LelikAgent(
            config=MagicMock(agent_id="lelik_agent_u1"), telephony=self.telephony, from_number="+15550102",
            status_callback_url="https://main.example.test/voice/status", prompt_builder=AsyncMock(),
            persona=AsyncMock(), notifications=AsyncMock(), voice_profile=VOICE_PROVIDER_PROFILES["openai"])
        agent.session_config = AsyncMock(return_value={"instructions": "persona", "tools": []})
        users = AsyncMock()
        users.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="acc-1")
        app = Quart(__name__)
        app.register_blueprint(create_voice_webhook_blueprint(
            user_repository=users, ephemeral_store=self.store, alert_sink=AsyncMock(),
            notification_service=AsyncMock(), lelik_agent_provider=AsyncMock(return_value=agent),
            answer_url="https://main.example.test/voice/answer", signature_verifier=AsyncMock(return_value=True),
            relay_stream_url="wss://relay.example.test/stream", cipher=self.cipher, digest_key=DIGEST_KEY))
        self.client = app.test_client()

    @property
    def docs(self) -> dict:
        return self.db.collection("voice_tickets").docs

    async def dial(self) -> str:
        """Dial in, end the dial; return the raw ticket (read from the pending record before the callback)."""
        await self.client.post("/voice/auth", form={"From": "+15550100", "CallSid": "CA0001"})
        record = self.docs["voice_pending_callback:CA0001"]["value"]
        ticket = await self.cipher.decrypt(record["ticket_ct"], aad="voice/voice_pending_callback:CA0001")
        await self.client.post("/voice/inbound-status", form={"CallSid": "CA0001", "CallStatus": "completed"})
        return ticket

    def urls(self) -> dict:
        kwargs = self.telephony.originate_call.await_args.kwargs
        return {"answer": kwargs["answer_url"], "status": kwargs["status_callback_url"]}


@pytest.fixture
def world():
    with eval_transactions():
        yield World()


def _path_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


@pytest.mark.asyncio
async def test_the_callback_urls_carry_no_raw_ticket(world):
    ticket = await world.dial()
    for url in world.urls().values():
        assert ticket not in url
        assert "ticket=" not in url
        assert "ref" in parse_qs(urlsplit(url).query)


@pytest.mark.asyncio
async def test_the_answer_callback_streams_the_ticket_it_decrypts(world):
    ticket = await world.dial()
    resp = await world.client.post(_path_query(world.urls()["answer"]), form={})
    body = await resp.get_data(as_text=True)
    assert f'<Parameter name="ticket" value="{ticket}"' in body


@pytest.mark.asyncio
async def test_a_terminal_status_releases_by_the_ref(world):
    ticket = await world.dial()
    assert ticket_key(DIGEST_KEY, ticket) in world.docs
    await world.client.post(_path_query(world.urls()["status"]), form={"CallStatus": "no-answer"})
    assert ticket_key(DIGEST_KEY, ticket) not in world.docs
    assert "voice_one_call:u1" not in world.docs


@pytest.mark.asyncio
async def test_an_unreadable_ref_is_rejected_and_releases_nothing(world):
    ticket = await world.dial()
    other = AesGcmTestCipher()
    foreign = await callback_ref_for(other, ticket)
    for bad in ("", "ref=", "ref=%2B%2F", f"ref={foreign}"):
        query = bad if bad.startswith("ref") else ""
        answer = await world.client.post(f"/voice/answer?{query}", form={})
        assert "<Reject" in await answer.get_data(as_text=True)
        status = await world.client.post(f"/voice/status?{query}", form={"CallStatus": "failed"})
        assert status.status_code == 200
    assert ticket_key(DIGEST_KEY, ticket) in world.docs
    assert "voice_one_call:u1" in world.docs


@pytest.mark.asyncio
async def test_a_raw_ticket_query_is_not_accepted(world):
    ticket = await world.dial()
    assert await ticket_of(world.cipher, parse_qs(urlsplit(world.urls()["answer"]).query)["ref"][0]) == ticket
    resp = await world.client.post(f"/voice/answer?ticket={ticket}", form={})
    assert "<Reject" in await resp.get_data(as_text=True)


@pytest.mark.asyncio
async def test_a_ref_sealed_under_another_label_is_refused_like_a_tampered_one(world):
    ticket = await world.dial()
    # Same key, wrong AAD: the label the stored ticket ciphertext uses.
    wrong_label = to_callback_ref(await world.cipher.encrypt(ticket, aad="voice/voice_pending_callback:CA0001"))
    answer = await world.client.post(f"/voice/answer?ref={wrong_label}", form={})
    assert "<Reject" in await answer.get_data(as_text=True)
    status = await world.client.post(f"/voice/status?ref={wrong_label}", form={"CallStatus": "failed"})
    assert status.status_code == 200
    assert ticket_key(DIGEST_KEY, ticket) in world.docs
    assert "voice_one_call:u1" in world.docs
