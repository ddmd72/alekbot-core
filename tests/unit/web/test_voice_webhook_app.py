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
    lelik_agent_factory.assert_called_once_with(user_id="u1", account_id="a1")
    lelik_agent.execute.assert_awaited_once()
    alert_sink.post.assert_not_called()

    notification_service.notify_raw.assert_awaited_once()
    _, notify_kwargs = notification_service.notify_raw.await_args
    assert notify_kwargs["user_id"] == "u1"
    assert notify_kwargs["account_id"] == "a1"
    assert isinstance(notify_kwargs["text"], str) and notify_kwargs["text"]


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
