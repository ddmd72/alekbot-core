"""Review fixes of the phone-replaces-web slice (VOICE_CALL_CONTROL_CHANNEL_RFC §7.5.4).

- The close of the replaced web call is capped and counted against the dial's one budget: a hung SFU cannot push
  `/voice/auth` past Twilio's deadline.
- A failing wait for the release means take over, not a 500.
- `/voice/session-config` refuses a ticket whose call was replaced (the marker names another owner) and leaves that
  marker alone; a phone call's own ticket redeems and extends its marker, end to end through the real routes.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.voice_ticket_keys import one_call_owner
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.services.voice_resume_service import VoiceResumeService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.voice_tickets import DIGEST_KEY, open_ticket
from tests.unit.web.test_voice_phone_replaces_web_call import AUTH, CALLER, MARKER, RELAY, SESSION, FakeMediaRoom

CIPHER = AesGcmTestCipher()
CALL_TTL_S = 3600


class TtlStore:
    """Ephemeral store keeping each key's last TTL; `fail_marker_reads_after` makes the n-th and later reads of the
    marker raise (a store outage mid-dial)."""

    def __init__(self):
        self.data, self.ttls = {}, {}
        self.marker_reads = 0
        self.fail_marker_reads_after = None

    async def set(self, key, value, ttl_s):
        self.data[key], self.ttls[key] = value, ttl_s

    async def get(self, key):
        if key == MARKER:
            self.marker_reads += 1
            if self.fail_marker_reads_after is not None and self.marker_reads > self.fail_marker_reads_after:
                raise RuntimeError("firestore unavailable")
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


class HangingMediaRoom(FakeMediaRoom):
    """An SFU whose close never answers."""

    def __init__(self):
        super().__init__()
        self.hang = False

    async def close(self, adapter_ids):
        if self.hang:
            await asyncio.Event().wait()
        await super().close(adapter_ids)


class World:
    def __init__(self, *, replace_wait_s=2.0, replace_close_s=1.5, room=None, phone_sees_room=True):
        self.store, self.room = TtlStore(), room or HangingMediaRoom()
        self.agent = MagicMock()
        self.agent.session_config = AsyncMock(return_value=dict(SESSION))
        self.agent.execute = AsyncMock()
        provider = AsyncMock(return_value=self.agent)
        users = AsyncMock()
        users.get_user_by_platform_id.return_value = MagicMock(user_id="usr-1", account_id="acc-1")
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": "usr-1", "account_id": "acc-1"})
        self.resume = VoiceResumeService(self.store)
        app = Quart(__name__)
        app.register_blueprint(create_voice_webhook_blueprint(
            user_repository=users, ephemeral_store=self.store, alert_sink=AsyncMock(),
            notification_service=AsyncMock(), lelik_agent_provider=provider,
            answer_url="https://main.example.com/voice/answer", signature_verifier=AsyncMock(return_value=True),
            relay_stream_url="wss://relay.example.com/", cipher=CIPHER, digest_key=DIGEST_KEY, one_call_ttl_s=CALL_TTL_S,
            media_room=self.room if phone_sees_room else None, replace_wait_s=replace_wait_s, replace_poll_s=0.01,
            replace_close_s=replace_close_s))
        app.register_blueprint(create_voice_web_call_blueprint(
            session_service=sessions,
            call_setup=VoiceCallSetupService(self.store, AsyncMock(), provider, digest_key=DIGEST_KEY),
            media_room=self.room, ephemeral_store=self.store, relay_base_url="wss://relay.example.com",
            cipher=CIPHER, digest_key=DIGEST_KEY, replace_poll_s=0.01))
        app.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
            summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
            digest_key=DIGEST_KEY, one_call_ttl_s=CALL_TTL_S, resume_service=self.resume))
        self.client = app.test_client()

    async def start_web(self):
        resp = await self.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
        assert resp.status_code == 201
        call_id = (await resp.get_json())["call_id"]
        key = f"voice_web_call:{call_id}"
        return call_id, await open_ticket(CIPHER, key, self.store.data[key])

    async def live_web_call(self):
        call_id, ticket = await self.start_web()
        await self.client.post(f"/api/voice/web-call/{call_id}/connect", headers=AUTH)
        assert (await self.session_config(ticket)).status_code == 200
        return call_id, ticket

    async def session_config(self, ticket):
        return await self.client.post("/voice/session-config", json={"ticket": ticket}, headers=RELAY)

    async def dial(self):
        resp = await self.client.post("/voice/auth", form={"From": CALLER, "CallSid": "CA0001"})
        return (await resp.get_data()).decode()

    async def phone_ticket(self):
        key = "voice_pending_callback:CA0001"
        return await open_ticket(CIPHER, key, self.store.data[key])


def _accepted(twiml):
    return "<Reject" not in twiml and "Calling you back" in twiml


# ----------------------------------------------------------------------------- the phone call's own ticket

@pytest.mark.asyncio
async def test_a_phone_call_redeems_its_own_ticket_and_its_marker_earns_the_call_ttl():
    """The marker's owner is the digest of the very ticket the relay redeems for the phone call: /voice/auth mints
    it, the callback carries it, /voice/answer prepares it and the relay presents it at session-config."""
    w = World()
    assert _accepted(await w.dial())
    await w.client.post("/voice/inbound-status", form={"CallSid": "CA0001", "CallStatus": "completed"})
    ticket = w.agent.execute.await_args.kwargs["ticket"]
    answered = await w.client.post(f"/voice/answer?ticket={ticket}", form={"CallSid": "CA0002"})
    assert "<Connect>" in (await answered.get_data()).decode()

    resp = await w.session_config(ticket)

    assert resp.status_code == 200 and (await resp.get_json())["call_kind"] == "phone"
    assert w.store.data[MARKER] == {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, ticket)}
    assert w.store.ttls[MARKER] == CALL_TTL_S


@pytest.mark.asyncio
async def test_a_phone_call_that_replaced_a_web_call_redeems_its_own_ticket():
    w = World(replace_wait_s=0.05)
    await w.live_web_call()
    assert _accepted(await w.dial())
    await w.client.post("/voice/inbound-status", form={"CallSid": "CA0001", "CallStatus": "completed"})
    ticket = w.agent.execute.await_args.kwargs["ticket"]
    await w.client.post(f"/voice/answer?ticket={ticket}", form={"CallSid": "CA0002"})

    resp = await w.session_config(ticket)

    assert resp.status_code == 200
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, ticket)
    assert w.store.ttls[MARKER] == CALL_TTL_S


# ----------------------------------------------------------------------------- a replaced web ticket

@pytest.mark.asyncio
async def test_a_replaced_web_calls_ticket_is_refused_at_session_config_and_leaves_the_phone_marker(caplog):
    """The web call is still in setup and the phone side cannot close it (no media room here), so its ticket is still
    redeemable when the relay presents it after the takeover."""
    w = World(replace_wait_s=0.05, phone_sees_room=False)
    _, web_ticket = await w.start_web()
    await w.resume.keep("usr-1", "conv-0001-invented", "lelik", "xai")
    assert _accepted(await w.dial())
    phone_marker, phone_ttl = dict(w.store.data[MARKER]), w.store.ttls[MARKER]

    resp = await w.session_config(web_ticket)

    assert resp.status_code == 404
    assert w.store.data[MARKER] == phone_marker and w.store.ttls[MARKER] == phone_ttl == 300
    assert "voice_resume:usr-1" in w.store.data  # the refused call took no resume handle
    assert any("replaced call" in r.getMessage() and web_ticket not in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------------- the close is capped, one budget

@pytest.mark.asyncio
async def test_a_hung_sfu_close_is_abandoned_at_its_cap_and_the_dial_takes_over(caplog):
    w = World(replace_wait_s=0.3, replace_close_s=0.05)
    web_call, web_ticket = await w.live_web_call()
    w.room.hang = True
    loop = asyncio.get_running_loop()
    started = loop.time()

    twiml = await asyncio.wait_for(w.dial(), timeout=2)

    assert _accepted(twiml)
    assert loop.time() - started < 1.0
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "took over" in r.getMessage()]
    assert len(warnings) == 1 and web_call in warnings[0].getMessage()
    # its ticket was redeemed before the takeover; the relay's late report leaves the phone call's marker
    phone_marker = dict(w.store.data[MARKER])
    await w.client.post("/voice/submit-transcript", headers=RELAY, json={
        "call_id": web_ticket, "user_id": "usr-1", "account_id": "acc-1", "transcript_text": "",
        "usage_by_model": {}, "turns": []})
    assert w.store.data[MARKER] == phone_marker


@pytest.mark.asyncio
async def test_the_close_counts_against_the_wait_budget_not_on_top_of_it():
    """A close cap larger than the whole budget is cut to the budget: the dial answers after about the budget."""
    w = World(replace_wait_s=0.2, replace_close_s=30.0)
    await w.live_web_call()
    w.room.hang = True
    loop = asyncio.get_running_loop()
    started = loop.time()

    twiml = await asyncio.wait_for(w.dial(), timeout=2)

    assert _accepted(twiml)
    assert loop.time() - started < 0.6


@pytest.mark.asyncio
async def test_a_hung_close_of_a_call_in_setup_leaves_its_ticket_to_be_refused_at_session_config():
    w = World(replace_wait_s=0.2, replace_close_s=0.05)
    web_call, web_ticket = await w.start_web()  # never connected: unredeemed, released only after the close
    w.room.hang = True

    assert _accepted(await asyncio.wait_for(w.dial(), timeout=2))
    phone_marker = dict(w.store.data[MARKER])

    assert (await w.session_config(web_ticket)).status_code == 404
    assert w.store.data[MARKER] == phone_marker


# ----------------------------------------------------------------------------- a failing wait

@pytest.mark.asyncio
async def test_a_store_error_while_waiting_for_the_release_means_take_over(caplog):
    w = World(replace_wait_s=2.0)
    web_call, _ = await w.live_web_call()
    w.store.fail_marker_reads_after = w.store.marker_reads + 1  # the dial's own read works; the wait's reads fail

    resp = await w.client.post("/voice/auth", form={"From": CALLER, "CallSid": "CA0001"})

    assert resp.status_code == 200 and _accepted((await resp.get_data()).decode())
    w.store.fail_marker_reads_after = None
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())
    assert any(r.levelno == logging.WARNING and "release failed" in r.getMessage() and web_call in r.getMessage()
               for r in caplog.records)
