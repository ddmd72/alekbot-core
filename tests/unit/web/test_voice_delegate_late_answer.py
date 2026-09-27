"""Late answers reach chat exactly once (voice UAT round 1, Task 2): /voice/delegate keeps the
result, /voice/delegate/abandon marks it abandoned, and whichever lands second posts it."""
import asyncio
from typing import Optional
from unittest.mock import AsyncMock

import pytest
from quart import Quart

import src.web.voice_control_plane_app as voice_control_plane_app
from src.domain.voice_delegation_outcome import VoiceDelegationOutcome
from src.ports.ephemeral_store import EphemeralStore
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint

_AUTH = {"Authorization": "Bearer x"}
_BODY = {"user_id": "u1", "account_id": "a1", "arguments": {"intent": "ask_alek", "query": "q"},
         "call_context": [], "ticket": "t1", "call_id": "c1", "request": "ask_alek: q"}
_RESULT_KEY = "voice_delegation_result:t1:c1"
_ABANDONED_KEY = "voice_delegation_abandoned:t1:c1"


class _MemoryStore(EphemeralStore):
    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def set(self, key: str, value: dict, ttl_s: int) -> None:
        self.data[key] = value
        self.ttls[key] = ttl_s

    async def get(self, key: str) -> Optional[dict]:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def get_and_delete(self, key: str) -> Optional[dict]:
        return self.data.pop(key, None)


def _app(store, sink, output="the answer", delegate_side_effect=None, failed=False):
    agent = AsyncMock()
    if delegate_side_effect is not None:
        agent.delegate_outcome.side_effect = delegate_side_effect
    else:
        agent.delegate_outcome.return_value = VoiceDelegationOutcome(output, failed)
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        lelik_agent_provider=AsyncMock(return_value=agent), late_answer_sink=sink,
    ))
    return app.test_client()


@pytest.mark.asyncio
async def test_finish_then_abandon_posts_once():
    store, sink = _MemoryStore(), AsyncMock()
    client = _app(store, sink)
    response = await client.post("/voice/delegate", json=_BODY, headers=_AUTH)
    assert (await response.get_json()) == {"output": "the answer"}
    sink.assert_not_awaited()

    response = await client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH)
    assert response.status_code == 200
    assert (await response.get_json()) == {"ok": True}
    sink.assert_awaited_once_with(user_id="u1", account_id="a1", request="ask_alek: q", output="the answer",
                                 failed=False)

    # A repeated abandon (timeout, then call end) finds nothing left to post.
    await client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH)
    assert sink.await_count == 1


@pytest.mark.asyncio
async def test_abandon_then_finish_posts_once():
    store, sink = _MemoryStore(), AsyncMock()
    client = _app(store, sink)
    await client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH)
    sink.assert_not_awaited()
    assert store.data[_ABANDONED_KEY] == {"abandoned": True}
    assert store.ttls[_ABANDONED_KEY] == 900

    response = await client.post("/voice/delegate", json=_BODY, headers=_AUTH)
    assert response.status_code == 200
    sink.assert_awaited_once_with(user_id="u1", account_id="a1", request="ask_alek: q", output="the answer",
                                 failed=False)
    assert _RESULT_KEY not in store.data


@pytest.mark.asyncio
async def test_finish_and_abandon_racing_post_once():
    store, sink = _MemoryStore(), AsyncMock()
    client = _app(store, sink)
    await asyncio.gather(
        client.post("/voice/delegate", json=_BODY, headers=_AUTH),
        client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH),
    )
    sink.assert_awaited_once()


@pytest.mark.asyncio
async def test_finish_without_abandon_keeps_the_result_and_posts_nothing():
    store, sink = _MemoryStore(), AsyncMock()
    response = await _app(store, sink).post("/voice/delegate", json=_BODY, headers=_AUTH)
    assert response.status_code == 200
    sink.assert_not_awaited()
    assert store.data[_RESULT_KEY] == {"output": "the answer", "request": "ask_alek: q",
                                       "user_id": "u1", "account_id": "a1"}
    assert store.ttls[_RESULT_KEY] == 900


@pytest.mark.asyncio
async def test_sink_error_still_answers_the_relay():
    store = _MemoryStore()
    await store.set(_ABANDONED_KEY, {"abandoned": True}, ttl_s=900)
    sink = AsyncMock(side_effect=RuntimeError("slack down"))
    response = await _app(store, sink).post("/voice/delegate", json=_BODY, headers=_AUTH)
    assert response.status_code == 200
    assert (await response.get_json()) == {"output": "the answer"}
    sink.assert_awaited_once()


@pytest.mark.asyncio
async def test_sink_error_on_abandon_still_returns_ok():
    store = _MemoryStore()
    sink = AsyncMock(side_effect=RuntimeError("slack down"))
    client = _app(store, sink)
    await client.post("/voice/delegate", json=_BODY, headers=_AUTH)
    response = await client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH)
    assert response.status_code == 200
    sink.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_error_still_answers_the_relay():
    store = AsyncMock()
    store.set.side_effect = RuntimeError("firestore down")
    response = await _app(store, AsyncMock()).post("/voice/delegate", json=_BODY, headers=_AUTH)
    assert response.status_code == 200
    assert (await response.get_json()) == {"output": "the answer"}


@pytest.mark.asyncio
async def test_failed_delegation_keeps_and_posts_nothing():
    store, sink = _MemoryStore(), AsyncMock()
    await store.set(_ABANDONED_KEY, {"abandoned": True}, ttl_s=900)
    response = await _app(store, sink, delegate_side_effect=RuntimeError("boom")).post(
        "/voice/delegate", json=_BODY, headers=_AUTH)
    assert response.status_code == 500
    assert _RESULT_KEY not in store.data
    sink.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegation_without_ticket_and_call_id_keeps_nothing():
    store, sink = _MemoryStore(), AsyncMock()
    body = {k: v for k, v in _BODY.items() if k not in ("ticket", "call_id")}
    response = await _app(store, sink).post("/voice/delegate", json=body, headers=_AUTH)
    assert response.status_code == 200
    assert store.data == {}


@pytest.mark.asyncio
async def test_abandon_requires_oidc():
    app = Quart(__name__)
    store = _MemoryStore()
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=False), alert_sink=AsyncMock(),
        late_answer_sink=AsyncMock(),
    ))
    response = await app.test_client().post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"})
    assert response.status_code == 401
    assert store.data == {}


@pytest.mark.asyncio
async def test_failed_delegation_result_is_kept_as_a_flag_and_posted_without_its_error_text():
    store, sink = _MemoryStore(), AsyncMock()
    client = _app(store, sink, output="SYSTEM: Specialist agent rejected: timeout", failed=True)
    response = await client.post("/voice/delegate", json=_BODY, headers=_AUTH)
    # The relay still hears the failure text (it speaks "it did not go through").
    assert (await response.get_json()) == {"output": "SYSTEM: Specialist agent rejected: timeout"}
    assert store.data[_RESULT_KEY] == {"failed": True, "request": "ask_alek: q",
                                       "user_id": "u1", "account_id": "a1"}

    await client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH)
    sink.assert_awaited_once_with(user_id="u1", account_id="a1", request="ask_alek: q", output="",
                                  failed=True)


@pytest.mark.asyncio
async def test_empty_answer_is_not_kept():
    store, sink = _MemoryStore(), AsyncMock()
    response = await _app(store, sink, output="").post("/voice/delegate", json=_BODY, headers=_AUTH)
    assert response.status_code == 200
    assert _RESULT_KEY not in store.data


@pytest.mark.asyncio
async def test_a_cancelled_handler_does_not_cancel_the_delegation():
    """Quart cancels a handler whose client disconnected; the relay disconnects when it stops
    waiting. The delegation must still finish and keep its result for chat."""
    store, sink = _MemoryStore(), AsyncMock()
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(**_):
        started.set()
        await release.wait()
        return VoiceDelegationOutcome("the answer", False)

    agent = AsyncMock()
    agent.delegate_outcome.side_effect = slow
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        lelik_agent_provider=AsyncMock(return_value=agent), late_answer_sink=sink,
    ))
    view = app.view_functions["voice_control_plane.delegate"]
    async with app.test_request_context("/voice/delegate", method="POST", json=_BODY, headers=_AUTH):
        # The view itself, awaited directly: cancelling it is what Quart does on http.disconnect.
        handler = asyncio.ensure_future(view())
        await asyncio.wait_for(started.wait(), 1)
        [inner] = list(voice_control_plane_app._IN_FLIGHT_DELEGATIONS)
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        assert handler.cancelled()
    assert not inner.cancelled() and not inner.done()

    await store.set(_ABANDONED_KEY, {"abandoned": True}, ttl_s=900)
    release.set()
    assert await asyncio.wait_for(inner, 1) == "the answer"
    sink.assert_awaited_once_with(user_id="u1", account_id="a1", request="ask_alek: q", output="the answer",
                                  failed=False)
    assert inner not in voice_control_plane_app._IN_FLIGHT_DELEGATIONS
