"""The interpreter rule (owner, 2026-11-10: an interpreter call has no memory and never reports to chat) holds with the
VOICE_MCP_TOOLS knob off: the knob gates minting, the call mode is read for every delegation."""
import logging
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.domain.voice_delegation_outcome import VoiceDelegationOutcome
from src.domain.voice_mode import VoiceMode
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.voice_tickets import call_kind_key
from tests.unit.web.test_voice_delegate_late_answer import _AUTH, _BODY, _RESULT_KEY, _MemoryStore
from tests.unit.web.test_voice_delegate_mcp_allowlist import _delegate, transactions  # noqa: F401


async def _late(mode, caplog):
    store, sink = _MemoryStore(), AsyncMock()
    await store.set(call_kind_key("t1"), {"mode": mode}, ttl_s=900)
    agent = AsyncMock()
    agent.delegate_outcome.return_value = VoiceDelegationOutcome("the answer", False)
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        lelik_agent_provider=AsyncMock(return_value=agent), late_answer_sink=sink, digest_key=b"k" * 32,
        voice_mcp_minting=False))
    client = app.test_client()
    await client.post("/voice/delegate", json=_BODY, headers=_AUTH)
    with caplog.at_level(logging.INFO):
        await client.post("/voice/delegate/abandon", json={"ticket": "t1", "call_id": "c1"}, headers=_AUTH)
    return store, sink


@pytest.mark.asyncio
async def test_with_the_knob_off_an_interpreter_call_refuses_a_delegation():
    output, dispatched = await _delegate(VoiceMode.INTERPRETER, minting=False)

    assert output.startswith("Tool error:")
    dispatched.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_the_knob_off_an_interpreter_late_answer_is_not_posted(caplog):
    store, sink = await _late("interpreter", caplog)

    sink.assert_not_awaited()
    assert _RESULT_KEY not in store.data
    assert "dropped: interpreter mode" in caplog.text


@pytest.mark.asyncio
async def test_with_the_knob_off_a_companion_late_answer_is_posted(caplog):
    _, sink = await _late("lelik", caplog)

    sink.assert_awaited_once()
