"""Persona assembly at the /voice/answer call site.

What goes INTO Lelik's prompt is LelikPersonaService's contract
(tests/unit/services/test_lelik_persona_service.py). This file covers the seam:

1. The webhook builds the persona for the identity the ticket resolved to.
2. The failure path. Persona assembly fails closed by repo convention (no
   fallback prompts), and Lelik's Firestore prompt content is uploaded by hand,
   so "token not uploaded yet" is a *routine* state, not an exotic one. A
   ticket/marker left behind by a failed assembly locks the caller out of any
   retry for up to `one_call_ttl_s`. A failure to build the service itself (no
   profile) takes the same path — never a silent empty-context call.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.voice_webhook_app import create_voice_webhook_blueprint


def _app(*, ephemeral_store, lelik_agent_provider, alert_sink):
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=alert_sink,
        notification_service=AsyncMock(),
        lelik_agent_provider=lelik_agent_provider,
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=True),
        relay_stream_url="wss://relay.example.com/",
    ))
    return app


def _store():
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    return ephemeral_store


def _persona(**session_kwargs):
    agent = MagicMock()
    agent.session_config = AsyncMock(**session_kwargs)
    return agent


async def _assert_failed_closed(response, ephemeral_store, alert_sink):
    # Graceful TwiML, not a bare 500 to Twilio, and no session opened.
    assert response.status_code == 200
    body = (await response.get_data()).decode()
    assert "<Response" in body
    assert "<Connect" not in body

    deleted_keys = {c.args[0] for c in ephemeral_store.delete.await_args_list}
    assert "voice_ticket:t1" in deleted_keys
    assert "voice_one_call:u1" in deleted_keys

    # Nothing half-written back onto the ticket.
    ephemeral_store.set.assert_not_called()
    alert_sink.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_webhook_builds_the_persona_for_the_ticket_identity():
    ephemeral_store = _store()
    agent = _persona(return_value={"instructions": "you are Lelik.", "tools": []})
    factory = AsyncMock(return_value=agent)

    client = _app(
        ephemeral_store=ephemeral_store, lelik_agent_provider=factory, alert_sink=AsyncMock(),
    ).test_client()
    await client.post("/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"})

    factory.assert_awaited_once_with("u1")
    agent.session_config.assert_awaited_once_with(user_id="u1", account_id="a1")


@pytest.mark.asyncio
async def test_persona_assembly_failure_releases_ticket_and_marker_and_alerts():
    ephemeral_store = _store()
    alert_sink = AsyncMock()
    agent = _persona(side_effect=KeyError("Blueprint not found: lelik_agent_v1"))

    client = _app(
        ephemeral_store=ephemeral_store,
        lelik_agent_provider=AsyncMock(return_value=agent),
        alert_sink=alert_sink,
    ).test_client()
    response = await client.post(
        "/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"},
    )

    await _assert_failed_closed(response, ephemeral_store, alert_sink)


@pytest.mark.asyncio
async def test_persona_service_construction_failure_takes_the_same_graceful_path():
    """No profile / config load failure happens inside the factory — same release,
    same alert, same apology TwiML as an assembly failure."""
    ephemeral_store = _store()
    alert_sink = AsyncMock()

    client = _app(
        ephemeral_store=ephemeral_store,
        lelik_agent_provider=AsyncMock(side_effect=ValueError("no user profile for u1")),
        alert_sink=alert_sink,
    ).test_client()
    response = await client.post(
        "/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"},
    )

    await _assert_failed_closed(response, ephemeral_store, alert_sink)
