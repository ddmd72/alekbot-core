"""A late answer of a voice delegation reaches the chat only from a companion call and only when it succeeded: an
interpreter call never reports to the chat, and a late failure goes nowhere in any mode (owner rules)."""
import logging
from unittest.mock import AsyncMock

import pytest

from tests.support.voice_tickets import call_kind_key
from quart import Quart

from src.domain.voice_delegation_outcome import VoiceDelegationOutcome
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.unit.web.test_voice_delegate_late_answer import _AUTH, _BODY, _RESULT_KEY, _MemoryStore

_ABANDON = {"ticket": "t1", "call_id": "c1"}


def _app(store, sink, failed):
    """The delegation route with the VOICE_MCP_TOOLS knob on: the only case that reads (and so stores) the call mode."""
    agent = AsyncMock()
    agent.delegate_outcome.return_value = VoiceDelegationOutcome("the answer", failed)
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        lelik_agent_provider=AsyncMock(return_value=agent), late_answer_sink=sink, digest_key=b"k" * 32,
        voice_mcp_minting=True))
    return app.test_client()


async def _late(mode, failed, caplog):
    store, sink = _MemoryStore(), AsyncMock()
    await store.set(call_kind_key("t1"), {"mode": mode}, ttl_s=900)
    client = _app(store, sink, failed)
    await client.post("/voice/delegate", json=_BODY, headers=_AUTH)
    with caplog.at_level(logging.INFO):
        await client.post("/voice/delegate/abandon", json=_ABANDON, headers=_AUTH)
    return store, sink


@pytest.mark.asyncio
async def test_an_interpreter_late_answer_is_not_posted(caplog):
    store, sink = await _late("interpreter", False, caplog)

    sink.assert_not_awaited()
    assert _RESULT_KEY not in store.data
    assert "dropped: interpreter mode" in caplog.text


@pytest.mark.asyncio
async def test_an_interpreter_late_failure_is_not_posted(caplog):
    _, sink = await _late("interpreter", True, caplog)

    sink.assert_not_awaited()
    assert "dropped: interpreter mode" in caplog.text


@pytest.mark.asyncio
async def test_a_companion_late_answer_is_still_posted(caplog):
    _, sink = await _late("lelik", False, caplog)

    sink.assert_awaited_once_with(user_id="u1", account_id="a1", request="ask_alek: q", output="the answer",
                                  failed=False)


@pytest.mark.asyncio
async def test_a_companion_late_failure_is_not_posted(caplog):
    store, sink = await _late("lelik", True, caplog)

    sink.assert_not_awaited()
    assert _RESULT_KEY not in store.data
    assert "dropped: failed" in caplog.text


@pytest.mark.asyncio
async def test_a_tutor_late_answer_is_still_posted(caplog):
    _, sink = await _late("tutor", False, caplog)

    sink.assert_awaited_once()
