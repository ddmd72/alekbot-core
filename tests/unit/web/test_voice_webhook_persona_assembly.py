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
3. The RFC §4.8 domain scoping of the biographical read, and the fact that a
   failed fact fetch is treated as a persona-assembly failure rather than
   degrading to an empty context.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.entities import FactDomain
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.repository import FactRepository
from src.web.voice_webhook_app import _LELIK_FACT_DOMAINS, create_voice_webhook_blueprint


def _facts() -> list:
    """A cache slice mixing the four allowed domains with domains Lelik must not see."""
    return [
        {"domain": FactDomain.BIOGRAPHICAL.value, "text": "allowed: bio"},
        {"domain": FactDomain.PREFERENCE.value, "text": "allowed: preference"},
        {"domain": FactDomain.LOCATION.value, "text": "allowed: location"},
        {"domain": FactDomain.AGENT_DIRECTIVE.value, "text": "allowed: directive"},
        {"domain": FactDomain.MEDICAL_RECORDS.value, "text": "denied: medical"},
        {"domain": FactDomain.FINANCE.value, "text": "denied: finance"},
        {"domain": FactDomain.WORK.value, "text": "denied: work"},
        {"domain": FactDomain.NETWORK.value, "text": "denied: network"},
    ]


def _app(*, ephemeral_store, prompt_builder, alert_sink, fact_repository=None):
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=alert_sink,
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        prompt_builder=prompt_builder,
        fact_repository=fact_repository if fact_repository is not None else AsyncMock(),
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


@pytest.mark.asyncio
async def test_biographical_read_is_scoped_to_the_four_allowed_domains():
    """RFC §4.8: Lelik gets 'a small explicit list of fact domains ... enough for light
    continuity and small talk, not a substitute for forwarding' — not the whole cache.

    `build_for_agent` has no `session_domains` kwarg; the override point is
    `biographical_facts`, so the webhook does the scoped read itself.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    fact_repository = AsyncMock(spec=FactRepository)
    fact_repository.get_biographical_context_cached.return_value = _facts()
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = "you are Lelik."

    client = _app(
        ephemeral_store=ephemeral_store,
        prompt_builder=prompt_builder,
        alert_sink=AsyncMock(),
        fact_repository=fact_repository,
    ).test_client()
    await client.post("/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "t1"})

    # Facts belong to the account, not the user (PromptBuilder's own strict separation).
    fact_repository.get_biographical_context_cached.assert_awaited_once_with("a1")

    passed = prompt_builder.build_for_agent.await_args.kwargs["biographical_facts"]
    assert {f["domain"] for f in passed} == {
        FactDomain.BIOGRAPHICAL.value,
        FactDomain.PREFERENCE.value,
        FactDomain.LOCATION.value,
        FactDomain.AGENT_DIRECTIVE.value,
    }
    assert all("denied" not in f["text"] for f in passed)
    # The filter kept every allowed fact, not just one per domain.
    assert len(passed) == 4


@pytest.mark.asyncio
async def test_scoped_domains_include_agent_directive_so_the_backstop_survives():
    """`PromptBuilder.build_for_agent` extracts the standing-directive block by filtering
    the SAME list passed as `biographical_facts`. If AGENT_DIRECTIVE were ever dropped
    from the scoped list, RFC §4.2's backstop would silently die while
    `include_directives=True` still read as if it were on. This test is the tripwire.
    """
    assert FactDomain.AGENT_DIRECTIVE.value in _LELIK_FACT_DOMAINS
    assert _LELIK_FACT_DOMAINS == {
        FactDomain.BIOGRAPHICAL.value,
        FactDomain.PREFERENCE.value,
        FactDomain.LOCATION.value,
        FactDomain.AGENT_DIRECTIVE.value,
    }


@pytest.mark.asyncio
async def test_fact_fetch_failure_takes_the_same_graceful_path_as_assembly_failure():
    """A repository failure is a persona-assembly failure — same ticket/marker release,
    same alert, same apology TwiML. Never a silent empty-context call.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    alert_sink = AsyncMock()
    fact_repository = AsyncMock(spec=FactRepository)
    fact_repository.get_biographical_context_cached.side_effect = RuntimeError("firestore down")
    prompt_builder = AsyncMock(spec=PromptBuilderPort)

    client = _app(
        ephemeral_store=ephemeral_store,
        prompt_builder=prompt_builder,
        alert_sink=alert_sink,
        fact_repository=fact_repository,
    ).test_client()
    response = await client.post(
        "/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "t1"},
    )

    assert response.status_code == 200
    body = (await response.get_data()).decode()
    assert "<Response" in body
    assert "<Connect" not in body

    # No degraded call opened on an empty context.
    prompt_builder.build_for_agent.assert_not_called()

    deleted_keys = {c.args[0] for c in ephemeral_store.delete.await_args_list}
    assert "voice_ticket:t1" in deleted_keys
    assert "voice_one_call:u1" in deleted_keys
    ephemeral_store.set.assert_not_called()
    alert_sink.post.assert_awaited_once()
