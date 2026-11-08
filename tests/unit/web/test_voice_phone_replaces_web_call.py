"""A phone dial replaces the same user's running web call instead of being rejected (VOICE_CALL_CONTROL_CHANNEL_RFC
§7.5.4, owner decision 2026-11-08: the phone is the urgent fallback, a hung web call must never block it). A phone call
is never replaced: a second dial is still rejected.

The Twilio webhook, the web-call blueprint and the real control-plane routes run on one store; a fake media room plays
the relay's part: closing a web call's adapters makes the "relay" post that call's end-of-call report (or not, for a
relay that does not answer within the phone's bounded wait).
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.domain.voice_ticket_keys import one_call_owner
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.services.voice_resume_service import VoiceResumeService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from src.web.voice_webhook_app import create_voice_webhook_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.voice_tickets import DIGEST_KEY, open_ticket, ticket_key

AUTH = {"Authorization": "Bearer tok"}
RELAY = {"Authorization": "Bearer relay"}
CIPHER = AesGcmTestCipher()
CALLER = "+15550101"
MARKER = "voice_one_call:usr-1"
SESSION = {"instructions": "you are Lelik", "tools": [],
           "voice": {"provider": "xai", "turn_ownership": "provider", "voice": "rex", "reasoning_effort": "low"}}


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


class FakeMediaRoom:
    """Each started web call gets its own adapters; `on_close` plays the relay seeing a call's egress gone."""

    def __init__(self, fails_to_close: bool = False):
        self._n = 0
        self.closed: list = []
        self.on_close = None
        self.fails_to_close = fails_to_close

    async def open_caller(self, sdp, mid):
        self._n += 1
        return CallerLeg(f"S{self._n}", "ANSWER")

    async def attach_agent(self, session_id, ingest_url, egress_url):
        return AgentLeg([f"{session_id}-in", f"{session_id}-eg"], "OFFER2")

    async def complete_negotiation(self, session_id, sdp):
        pass

    async def close(self, adapter_ids):
        if self.fails_to_close:
            raise RuntimeError("SFU unavailable")
        self.closed.append(list(adapter_ids))
        if self.on_close is not None and adapter_ids:
            self.on_close(list(adapter_ids))


class World:
    def __init__(self, *, replace_wait_s: float = 2.0, room: FakeMediaRoom = None, phone_sees_room: bool = True):
        self.store = MemoryStore()
        self.room = room or FakeMediaRoom()
        self.agent = MagicMock()
        self.agent.session_config = AsyncMock(return_value=dict(SESSION))
        self.agent.execute = AsyncMock()
        provider = AsyncMock(return_value=self.agent)
        users = AsyncMock()
        users.get_user_by_platform_id.return_value = MagicMock(user_id="usr-1", account_id="acc-1")
        sessions = MagicMock()
        sessions.verify_access_token = MagicMock(return_value={"sub": "usr-1", "account_id": "acc-1"})
        app = Quart(__name__)
        app.register_blueprint(create_voice_webhook_blueprint(
            user_repository=users, ephemeral_store=self.store, alert_sink=AsyncMock(),
            notification_service=AsyncMock(), lelik_agent_provider=provider,
            answer_url="https://main.example.com/voice/answer", signature_verifier=AsyncMock(return_value=True),
            relay_stream_url="wss://relay.example.com/", cipher=CIPHER, digest_key=DIGEST_KEY,
            media_room=self.room if phone_sees_room else None, replace_wait_s=replace_wait_s, replace_poll_s=0.01))
        app.register_blueprint(create_voice_web_call_blueprint(
            session_service=sessions,
            call_setup=VoiceCallSetupService(self.store, AsyncMock(), provider, digest_key=DIGEST_KEY),
            media_room=self.room, ephemeral_store=self.store, relay_base_url="wss://relay.example.com",
            cipher=CIPHER, digest_key=DIGEST_KEY, replace_poll_s=0.01))
        app.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
            summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
            digest_key=DIGEST_KEY, resume_service=VoiceResumeService(self.store)))
        self.client = app.test_client()

    async def start_web(self):
        resp = await self.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)
        assert resp.status_code == 201
        call_id = (await resp.get_json())["call_id"]
        return call_id, await open_ticket(CIPHER, f"voice_web_call:{call_id}", self.store.data[f"voice_web_call:{call_id}"])

    async def live_web_call(self):
        """A started, connected web call whose ticket the relay redeemed: a running call on the main side."""
        call_id, ticket = await self.start_web()
        await self.client.post(f"/api/voice/web-call/{call_id}/connect", headers=AUTH)
        assert (await self.client.post("/voice/session-config", json={"ticket": ticket}, headers=RELAY)).status_code == 200
        return call_id, ticket

    async def report(self, ticket):
        """The relay's end-of-call report for the call of `ticket`."""
        resp = await self.client.post("/voice/submit-transcript", headers=RELAY, json={
            "call_id": ticket, "user_id": "usr-1", "account_id": "acc-1", "transcript_text": "",
            "usage_by_model": {}, "turns": [], "resume_handle": "conv-0001-invented", "resume_provider": "xai"})
        assert resp.status_code == 200

    def relay_reports_after(self, ticket, after_s):
        tasks = []

        def on_close(_adapter_ids):
            async def later():
                await asyncio.sleep(after_s)
                await self.report(ticket)
            tasks.append(asyncio.ensure_future(later()))

        self.room.on_close = on_close
        return tasks

    async def dial(self, call_sid="CA0001"):
        form = {"From": CALLER}
        if call_sid:
            form["CallSid"] = call_sid
        resp = await self.client.post("/voice/auth", form=form)
        return (await resp.get_data()).decode()

    async def phone_ticket(self, call_sid="CA0001"):
        key = f"voice_pending_callback:{call_sid}"
        return await open_ticket(CIPHER, key, self.store.data[key])


def _accepted(twiml: str) -> bool:
    return "<Reject" not in twiml and "Calling you back" in twiml


@pytest.mark.asyncio
async def test_a_dial_closes_the_running_web_call_and_goes_on_once_it_released(caplog):
    caplog.set_level(logging.INFO)
    w = World()
    web_call, web_ticket = await w.live_web_call()
    reports = w.relay_reports_after(web_ticket, after_s=0.05)

    twiml = await w.dial()

    assert _accepted(twiml)
    assert w.room.closed == [["S1-in", "S1-eg"]]
    assert reports and all(task.done() for task in reports)  # the dial went on after the old report released
    assert w.store.data[MARKER] == {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, await w.phone_ticket())}
    lines = [r for r in caplog.records if "replaces the running web call" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert web_call in lines[0].getMessage() and "usr-1" in lines[0].getMessage()
    assert all(CALLER not in r.getMessage() and web_ticket not in r.getMessage() for r in lines)


@pytest.mark.asyncio
async def test_a_web_call_that_does_not_release_within_the_bound_is_taken_over(caplog):
    caplog.set_level(logging.INFO)
    w = World(replace_wait_s=0.1)
    web_call, _ = await w.live_web_call()  # nothing plays the relay: the old call never reports in time

    twiml = await asyncio.wait_for(w.dial(), timeout=2)

    assert _accepted(twiml)
    assert w.room.closed == [["S1-in", "S1-eg"]]
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())
    assert "call_id" not in w.store.data[MARKER]
    assert any("takes it over" in r.getMessage() and web_call in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_replaced_web_calls_late_report_leaves_the_phone_calls_marker():
    w = World(replace_wait_s=0.05)
    _, web_ticket = await w.live_web_call()
    await w.dial()
    phone_marker = dict(w.store.data[MARKER])

    await w.report(web_ticket)  # the old web call's end-of-call report, after the takeover

    assert w.store.data[MARKER] == phone_marker


@pytest.mark.asyncio
async def test_the_phone_calls_own_report_still_releases_its_marker():
    w = World(replace_wait_s=0.05)
    _, web_ticket = await w.live_web_call()
    await w.dial()
    await w.report(web_ticket)

    await w.report(await w.phone_ticket())

    assert MARKER not in w.store.data


@pytest.mark.asyncio
async def test_a_marker_lost_to_a_racing_late_release_is_rewritten_when_the_dial_ends():
    """The store has no compare-and-delete: a late release whose ownership read came just before the takeover can still
    delete the phone call's marker. The inbound dial's end rewrites the marker whole, owner included."""
    w = World(replace_wait_s=0.05)
    await w.live_web_call()
    await w.dial()
    phone_ticket = await w.phone_ticket()
    del w.store.data[MARKER]  # what the racing delete leaves

    await w.client.post("/voice/inbound-status", form={"CallSid": "CA0001", "CallStatus": "completed"})

    assert w.store.data[MARKER] == {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, phone_ticket)}
    w.agent.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_dial_during_a_phone_call_is_rejected_and_the_marker_untouched():
    w = World(replace_wait_s=30.0)
    assert _accepted(await w.dial("CA0001"))
    phone_marker = dict(w.store.data[MARKER])

    twiml = await asyncio.wait_for(w.dial("CA0002"), timeout=2)  # refused at once, not after the bound

    assert "<Reject" in twiml
    assert w.store.data[MARKER] == phone_marker
    assert "voice_pending_callback:CA0002" not in w.store.data
    assert w.room.closed == []


@pytest.mark.asyncio
async def test_a_marker_naming_an_empty_web_call_counts_as_a_phone_call():
    w = World()
    w.store.data[MARKER] = {"in_flight": True, "call_id": ""}

    assert "<Reject" in await w.dial()
    assert w.store.data[MARKER] == {"in_flight": True, "call_id": ""}


@pytest.mark.asyncio
async def test_a_web_call_still_in_setup_is_released_at_once_and_its_ticket_dies():
    w = World(replace_wait_s=30.0)
    _, web_ticket = await w.start_web()  # never connected: no adapters, the relay never redeemed the ticket

    twiml = await asyncio.wait_for(w.dial(), timeout=2)

    assert _accepted(twiml)
    assert ticket_key(web_ticket) not in w.store.data  # the old ticket can no longer start a relay call
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())


@pytest.mark.asyncio
async def test_a_hung_up_web_call_still_finishing_is_waited_for_with_nothing_to_close():
    w = World()
    web_call, web_ticket = await w.live_web_call()
    w.room.on_close = None  # the hangup closes the legs; the relay's report comes later, during the dial's wait
    await w.client.post(f"/api/voice/web-call/{web_call}/hangup", json={"trigger": "button"}, headers=AUTH)
    late = asyncio.ensure_future(_after(0.05, w.report(web_ticket)))

    twiml = await w.dial()

    assert _accepted(twiml) and late.done()
    assert w.room.closed == [["S1-in", "S1-eg"]]  # only the hangup's close: no record left to close again
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())


@pytest.mark.asyncio
async def test_a_failure_closing_the_web_call_does_not_block_the_dial(caplog):
    room = FakeMediaRoom()
    w = World(replace_wait_s=0.05, room=room)
    web_call, _ = await w.live_web_call()
    room.fails_to_close = True

    twiml = await w.dial()

    assert _accepted(twiml)
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())
    assert any(r.levelno == logging.ERROR and web_call in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_without_the_web_call_deployed_a_web_marker_is_only_waited_for_and_taken_over():
    w = World(replace_wait_s=0.05, phone_sees_room=False)
    await w.live_web_call()

    twiml = await w.dial()

    assert _accepted(twiml)
    assert w.room.closed == []
    assert w.store.data[MARKER]["owner"] == one_call_owner(DIGEST_KEY, await w.phone_ticket())


@pytest.mark.asyncio
async def test_a_dial_without_a_call_sid_is_rejected_before_it_touches_the_web_call():
    w = World()
    web_call, _ = await w.live_web_call()
    web_marker = dict(w.store.data[MARKER])

    assert "<Reject" in await w.dial(call_sid=None)
    assert w.room.closed == []
    assert w.store.data[MARKER] == web_marker and web_marker["call_id"] == web_call


@pytest.mark.asyncio
async def test_a_web_start_never_replaces_the_phone_call_that_replaced_a_web_call():
    w = World(replace_wait_s=0.05)
    await w.live_web_call()
    await w.dial()
    phone_marker = dict(w.store.data[MARKER])

    resp = await w.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"}, headers=AUTH)

    assert resp.status_code == 409
    assert w.store.data[MARKER] == phone_marker


async def _after(delay_s, coro):
    await asyncio.sleep(delay_s)
    await coro
