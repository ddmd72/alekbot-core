import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.voice_webhook_app import create_voice_webhook_blueprint


@pytest.fixture
def deps():
    user_repository = AsyncMock()
    ephemeral_store = AsyncMock()
    alert_sink = AsyncMock()
    notification_service = AsyncMock()
    lelik_agent = AsyncMock()
    lelik_agent_factory = MagicMock(return_value=lelik_agent)
    return (
        user_repository,
        ephemeral_store,
        alert_sink,
        notification_service,
        lelik_agent,
        lelik_agent_factory,
    )


def _app(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, _, lelik_agent_factory = deps
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository,
        ephemeral_store=ephemeral_store,
        alert_sink=alert_sink,
        notification_service=notification_service,
        lelik_agent_factory=lelik_agent_factory,
        answer_url="https://main.example.com/voice/answer",
    ))
    return app


@pytest.mark.asyncio
async def test_unbound_number_rejected_and_alerted_before_any_callback(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, _ = deps
    user_repository.get_user_by_platform_id.return_value = None

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    user_repository.get_user_by_platform_id.assert_awaited_once_with("phone", "+346001")
    alert_sink.post.assert_awaited_once()
    ephemeral_store.set.assert_not_called()
    lelik_agent.execute.assert_not_called()
    notification_service.notify_raw.assert_not_called()


@pytest.mark.asyncio
async def test_bound_number_mints_ticket_and_marker_then_originates_callback(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, lelik_agent_factory = deps
    user_repository.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
    ephemeral_store.get.return_value = None  # no existing one-call marker

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    body = (await response.get_data()).decode()
    assert "<Hangup" in body
    assert "<Reject" not in body

    set_calls = {c.args[0]: c.args[1] for c in ephemeral_store.set.await_args_list}
    assert any(key.startswith("voice_ticket:") for key in set_calls)
    assert "voice_one_call:u1" in set_calls
    lelik_agent_factory.assert_called_once_with(user_id="u1", account_id="a1", to_number="+346001")
    lelik_agent.execute.assert_awaited_once()
    alert_sink.post.assert_not_called()

    notification_service.notify_raw.assert_awaited_once()
    _, notify_kwargs = notification_service.notify_raw.await_args
    assert notify_kwargs["user_id"] == "u1"
    assert notify_kwargs["account_id"] == "a1"
    assert isinstance(notify_kwargs["text"], str) and notify_kwargs["text"]


@pytest.mark.asyncio
async def test_origination_failure_releases_ticket_and_marker_and_alerts(deps):
    """If lelik_agent.execute() raises (network blip, agent error, ...), the
    ticket and one-call marker minted just before the call must not be left
    stuck for up to one_call_ttl_s - this is the mint-side counterpart to
    Task 7's submit_transcript release-on-failure guarantee (commit
    d1dd648). The handler must also alert and return graceful TwiML, not a
    bare 500."""
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, lelik_agent_factory = deps
    user_repository.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
    ephemeral_store.get.return_value = None  # no existing one-call marker
    lelik_agent.execute.side_effect = RuntimeError("origination boom")

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    # Response must be graceful TwiML, not a crash.
    assert response.status_code == 200
    body = (await response.get_data()).decode()
    assert "<Response" in body

    set_calls = {c.args[0]: c.args[1] for c in ephemeral_store.set.await_args_list}
    ticket_key = next(key for key in set_calls if key.startswith("voice_ticket:"))

    deleted_keys = {c.args[0] for c in ephemeral_store.delete.await_args_list}
    assert ticket_key in deleted_keys
    assert "voice_one_call:u1" in deleted_keys

    alert_sink.post.assert_awaited_once()
    notification_service.notify_raw.assert_not_called()


@pytest.mark.asyncio
async def test_second_call_refused_while_one_already_in_flight(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, _ = deps
    user_repository.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
    ephemeral_store.get.return_value = {"in_flight": True}

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    lelik_agent.execute.assert_not_called()
    notification_service.notify_raw.assert_not_called()


@pytest.mark.asyncio
async def test_answer_webhook_hangs_up_on_machine_without_opening_session():
    user_repository = AsyncMock()
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    prompt_builder = AsyncMock()

    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository,
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        prompt_builder=prompt_builder,
        relay_stream_url="wss://relay.example.com/",
    ))
    client = app.test_client()

    response = await client.post("/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "machine_end_beep", "ticket": "t1"})

    body = (await response.get_data()).decode()
    assert "<Hangup" in body
    assert "<Connect" not in body
    prompt_builder.build_for_agent.assert_not_called()


@pytest.mark.asyncio
async def test_answer_webhook_assembles_persona_and_streams_on_human_pickup():
    user_repository = AsyncMock()
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "you are Lelik.\n<!-- CACHE_BOUNDARY -->\ndynamic"

    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository,
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        prompt_builder=prompt_builder,
        # Task 19 fix round: /voice/answer now does a domain-scoped biographical
        # read of its own before assembling the persona (RFC §4.8), so this route
        # needs the repository injected. Mechanical adaptation to a new dependency;
        # no assertion in this test changed.
        fact_repository=AsyncMock(),
        relay_stream_url="wss://relay.example.com/",
    ))
    client = app.test_client()

    response = await client.post("/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "t1"})

    body = (await response.get_data()).decode()
    assert "<Connect>" in body
    assert "wss://relay.example.com/" in body
    assert "t1" in body
    prompt_builder.build_for_agent.assert_awaited_once()
    stashed = ephemeral_store.set.await_args.args
    assert stashed[0] == "voice_ticket:t1"
    assert "instructions" in stashed[1]


@pytest.mark.asyncio
async def test_answer_webhook_rejects_when_ticket_not_found():
    """A redeemed/expired/forged ticket has no identity behind it in the
    EphemeralStore - reject the call rather than opening a session with no
    known user_id/account_id."""
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = None
    prompt_builder = AsyncMock()

    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        prompt_builder=prompt_builder,
        relay_stream_url="wss://relay.example.com/",
    ))
    client = app.test_client()

    response = await client.post("/voice/answer", form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "unknown"})

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    prompt_builder.build_for_agent.assert_not_called()
