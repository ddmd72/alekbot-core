"""Persona assembly at the /voice/answer call site (Task 19).

Two things Task 9's own webhook tests do not cover:

1. The *shape* of the `build_for_agent` call — RFC §4.8 makes Lelik's read-side
   permissions an explicit decision (biographical access on, unlike the tutor
   which turns it off; standing directives on because §4.2 leans on the rulebook
   as the backstop if the persona's forwarding discipline slips). Asserted the
   same way `tests/unit/agents/test_tutor_agent.py` asserts the tutor's toggles:
   kwargs against a mocked `PromptBuilderPort` at the real call site.
2. The failure path. `build_for_agent` fails closed by repo convention (no
   fallback prompts), and Lelik's Firestore prompt content is uploaded by hand,
   so "token not uploaded yet" is a *routine* state, not an exotic one. A
   ticket/marker left behind by a failed assembly locks the caller out of any
   retry for up to `one_call_ttl_s`.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.ports.prompt_builder_port import PromptBuilderPort
from src.web.voice_webhook_app import create_voice_webhook_blueprint


def _app(*, ephemeral_store, prompt_builder, alert_sink):
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=alert_sink,
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        prompt_builder=prompt_builder,
        relay_stream_url="wss://relay.example.com/",
    ))
    return app


@pytest.mark.asyncio
async def test_answer_webhook_requests_lelik_persona_with_explicit_read_toggles():
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = "you are Lelik."

    client = _app(
        ephemeral_store=ephemeral_store,
        prompt_builder=prompt_builder,
        alert_sink=AsyncMock(),
    ).test_client()
    await client.post("/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "t1"})

    kwargs = prompt_builder.build_for_agent.await_args.kwargs
    assert kwargs["agent_type"] == "lelik"
    assert kwargs["user_id"] == "u1"
    assert kwargs["account_id"] == "a1"
    # RFC §4.8: biographical access is scoped, not off (contrast the tutor).
    assert kwargs["include_biographical"] is True
    # RFC §4.2: standing directives are the named backstop for forwarding discipline.
    assert kwargs["include_directives"] is True


@pytest.mark.asyncio
async def test_persona_assembly_failure_releases_ticket_and_marker_and_alerts():
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    alert_sink = AsyncMock()
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.side_effect = KeyError("Blueprint not found: lelik_agent_v1")

    client = _app(
        ephemeral_store=ephemeral_store,
        prompt_builder=prompt_builder,
        alert_sink=alert_sink,
    ).test_client()
    response = await client.post(
        "/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "t1"},
    )

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
