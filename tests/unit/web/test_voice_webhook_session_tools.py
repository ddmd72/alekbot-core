import pytest
from unittest.mock import AsyncMock

from quart import Quart

from src.web.voice_webhook_app import create_voice_webhook_blueprint

_TOOL = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}


def _app(ephemeral_store, provider, alert_sink=None):
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(), ephemeral_store=ephemeral_store, alert_sink=alert_sink or AsyncMock(),
        notification_service=AsyncMock(), lelik_agent_provider=provider,
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=True), relay_stream_url="wss://relay.example.com/",
    ))
    return app


@pytest.mark.asyncio
async def test_answer_stores_instructions_and_tools_on_the_ticket():
    store = AsyncMock()
    store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    agent = AsyncMock()
    agent.session_config.return_value = {"instructions": "PROMPT", "tools": [_TOOL]}
    client = _app(store, AsyncMock(return_value=agent)).test_client()
    await client.post("/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"})
    stored = store.set.await_args.args[1]
    assert stored == {"user_id": "u1", "account_id": "a1", "instructions": "PROMPT", "tools": [_TOOL]}


@pytest.mark.asyncio
async def test_answer_ticket_identity_wins_over_session_keys():
    # /voice/delegate trusts user_id/account_id from the stored ticket - a session_config
    # that happens to echo back a user_id key must never override the caller's real identity.
    store = AsyncMock()
    store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    agent = AsyncMock()
    agent.session_config.return_value = {"instructions": "PROMPT", "tools": [_TOOL], "user_id": "evil"}
    client = _app(store, AsyncMock(return_value=agent)).test_client()
    await client.post("/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"})
    stored = store.set.await_args.args[1]
    assert stored["user_id"] == "u1"


@pytest.mark.asyncio
async def test_answer_with_no_lelik_available_fails_closed():
    store = AsyncMock()
    store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    alert_sink = AsyncMock()
    client = _app(store, AsyncMock(return_value=None), alert_sink).test_client()
    response = await client.post("/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"})
    assert "<Connect" not in (await response.get_data()).decode()
    assert {c.args[0] for c in store.delete.await_args_list} >= {"voice_ticket:t1", "voice_one_call:u1"}
    alert_sink.post.assert_awaited_once()
