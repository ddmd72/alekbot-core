"""A new web call replaces the user's running one instead of answering 409 (VOICE_CALL_CONTROL_CHANNEL_RFC §7.5,
slice 7): the running call's media legs close, the start waits (bounded) for its one-call marker to be released, then
claims. The replaced call is not a button hangup, so it keeps its resume handle and the new call resumes it.

The resumption tests run the web-call blueprint and the real control-plane routes on one store; a fake media room
plays the relay's part: closing a call's adapters makes the "relay" post that call's end-of-call report.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.media_room_agent_leg import AgentLeg
from src.domain.media_room_caller_leg import CallerLeg
from src.domain.voice_ticket_keys import button_end_key
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.services.voice_resume_service import VoiceResumeService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.voice_tickets import DIGEST_KEY, open_ticket, ticket_key

AUTH = {"Authorization": "Bearer tok"}
RELAY = {"Authorization": "Bearer relay"}
CIPHER = AesGcmTestCipher()
HANDLE = "conv-0001-invented"
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
    """Each started call gets its own adapters; `on_close` plays the relay seeing a call's egress gone."""

    def __init__(self):
        self._n = 0
        self.closed: list = []
        self.on_close = None

    async def open_caller(self, sdp, mid):
        self._n += 1
        return CallerLeg(f"S{self._n}", "ANSWER")

    async def attach_agent(self, session_id, ingest_url, egress_url):
        return AgentLeg([f"{session_id}-in", f"{session_id}-eg"], "OFFER2")

    async def complete_negotiation(self, session_id, sdp):
        pass

    async def close(self, adapter_ids):
        self.closed.append(list(adapter_ids))
        if self.on_close is not None and adapter_ids:
            self.on_close(list(adapter_ids))


def _app(store, room, *, user="usr-1", replace_wait_s=5.0):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": user, "account_id": "acc-1"})
    agent = MagicMock()
    agent.session_config = AsyncMock(return_value=dict(SESSION))
    app = Quart(__name__)
    app.register_blueprint(create_voice_web_call_blueprint(
        session_service=session_service,
        call_setup=VoiceCallSetupService(store, AsyncMock(), AsyncMock(return_value=agent), digest_key=DIGEST_KEY),
        media_room=room, ephemeral_store=store, relay_base_url="wss://relay.example.com", cipher=CIPHER,
        digest_key=DIGEST_KEY, replace_wait_s=replace_wait_s, replace_poll_s=0.01))
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        digest_key=DIGEST_KEY, resume_service=VoiceResumeService(store)))
    return app


async def _start(client, mode="interpreter"):
    resp = await client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0", "mode": mode}, headers=AUTH)
    return resp, await resp.get_json()


async def _ticket(store, call_id):
    key = f"voice_web_call:{call_id}"
    return await open_ticket(CIPHER, key, store.data[key])


async def _live_call(client, store):
    """A started, connected call whose ticket the relay redeemed: what a running call looks like on the main side."""
    _, body = await _start(client)
    await client.post(f"/api/voice/web-call/{body['call_id']}/connect", headers=AUTH)
    ticket = await _ticket(store, body["call_id"])
    redeemed = await client.post("/voice/session-config", json={"ticket": ticket}, headers=RELAY)
    assert redeemed.status_code == 200
    return body["call_id"], ticket


def _relay_reports(client, ticket, *, after_s=0.05, handle=HANDLE):
    """The relay's part once the call's adapters close: after the re-attach window, its end-of-call report."""
    tasks = []

    async def report():
        await asyncio.sleep(after_s)
        await client.post("/voice/submit-transcript", headers=RELAY, json={
            "call_id": ticket, "user_id": "usr-1", "account_id": "acc-1", "transcript_text": "",
            "usage_by_model": {}, "turns": [], "resume_handle": handle, "resume_provider": "xai"})

    def on_close(_adapter_ids):
        tasks.append(asyncio.ensure_future(report()))

    return on_close, tasks


@pytest.mark.asyncio
async def test_a_new_call_closes_the_running_one_waits_for_its_release_and_starts(caplog):
    caplog.set_level(logging.INFO)
    store, room = MemoryStore(), FakeMediaRoom()
    client = _app(store, room).test_client()
    old_call, old_ticket = await _live_call(client, store)
    room.on_close, reports = _relay_reports(client, old_ticket)

    resp, body = await _start(client)

    assert resp.status_code == 201
    assert room.closed == [["S1-in", "S1-eg"]]
    assert reports and all(task.done() for task in reports)  # started only once the old report had released
    assert store.data["voice_one_call:usr-1"]["call_id"] == body["call_id"] != old_call
    # Not a button end: nothing marks the replaced call as never-resume.
    assert button_end_key(DIGEST_KEY, old_ticket) not in store.data
    lines = [r.getMessage() for r in caplog.records if "replacing the running call" in r.getMessage()]
    assert len(lines) == 1 and old_call in lines[0] and old_ticket not in lines[0]


@pytest.mark.asyncio
async def test_a_redial_during_a_live_call_resumes_it():
    store, room = MemoryStore(), FakeMediaRoom()
    client = _app(store, room).test_client()
    _, old_ticket = await _live_call(client, store)
    room.on_close, _ = _relay_reports(client, old_ticket)

    _, body = await _start(client)
    new_ticket = await _ticket(store, body["call_id"])
    config = await (await client.post("/voice/session-config", json={"ticket": new_ticket}, headers=RELAY)).get_json()

    assert config["resume"] == {"handle": HANDLE}
    assert "voice_resume:usr-1" not in store.data  # one resume per handle


@pytest.mark.asyncio
async def test_after_a_button_hangup_the_next_call_starts_fresh():
    store, room = MemoryStore(), FakeMediaRoom()
    client = _app(store, room).test_client()
    old_call, old_ticket = await _live_call(client, store)
    room.on_close, reports = _relay_reports(client, old_ticket, after_s=0)
    await client.post(f"/api/voice/web-call/{old_call}/hangup", json={"trigger": "button"}, headers=AUTH)
    await asyncio.gather(*reports)
    assert "voice_resume:usr-1" not in store.data

    resp, body = await _start(client)
    new_ticket = await _ticket(store, body["call_id"])
    config = await (await client.post("/voice/session-config", json={"ticket": new_ticket}, headers=RELAY)).get_json()

    assert resp.status_code == 201
    assert "resume" not in config


@pytest.mark.asyncio
async def test_a_marker_never_released_within_the_bound_is_409():
    store, room = MemoryStore(), FakeMediaRoom()
    client = _app(store, room, replace_wait_s=0.1).test_client()
    old_call, _ = await _live_call(client, store)  # nothing plays the relay: the old call never reports

    resp, body = await _start(client)

    assert resp.status_code == 409 and body["error"] == "a call is already in progress"
    assert room.closed == [["S1-in", "S1-eg"]]
    assert store.data["voice_one_call:usr-1"]["call_id"] == old_call


@pytest.mark.asyncio
async def test_a_phone_call_is_never_replaced():
    store, room = MemoryStore(), FakeMediaRoom()
    store.data["voice_one_call:usr-1"] = {"in_flight": True}  # the phone path's marker names no web call
    client = _app(store, room, replace_wait_s=30.0).test_client()

    resp, _ = await asyncio.wait_for(_start(client), timeout=2)  # refused at once, not after the bound

    assert resp.status_code == 409
    assert room.closed == []
    assert store.data["voice_one_call:usr-1"] == {"in_flight": True}


@pytest.mark.asyncio
async def test_the_marker_is_per_user_so_another_users_call_is_left_alone():
    store, room = MemoryStore(), FakeMediaRoom()
    first = _app(store, room).test_client()
    running_call, _ = await _live_call(first, store)

    resp, body = await _start(_app(store, room, user="usr-2").test_client())

    assert resp.status_code == 201
    assert room.closed == []
    assert store.data["voice_one_call:usr-1"]["call_id"] == running_call
    assert store.data["voice_one_call:usr-2"]["call_id"] == body["call_id"]


@pytest.mark.asyncio
async def test_a_call_still_in_setup_is_released_at_once_and_replaced():
    store, room = MemoryStore(), FakeMediaRoom()
    client = _app(store, room, replace_wait_s=30.0).test_client()
    _, old = await _start(client)  # never connected: no adapters, the relay never redeemed the ticket
    old_ticket = await _ticket(store, old["call_id"])

    resp, body = await asyncio.wait_for(_start(client), timeout=2)

    assert resp.status_code == 201
    assert ticket_key(old_ticket) not in store.data  # the old ticket can no longer start a relay call
    assert store.data["voice_one_call:usr-1"]["call_id"] == body["call_id"]
