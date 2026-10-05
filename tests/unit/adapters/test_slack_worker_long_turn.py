"""Slack worker: lock released by the long-turn hook; metadata carries turn identity."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.language import LanguageCode


def _authorized_decision():
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    return decision


@pytest.fixture
def slack_adapter_with_handler_spy():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()

    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = conversation_handler
    adapter.iam_service = iam_service
    adapter.app = MagicMock()
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._resolve_language = AsyncMock(return_value=(LanguageCode.EN, None, True))

    return adapter, conversation_handler


async def test_metadata_carries_turn_identity(slack_adapter_with_handler_spy):
    adapter, handler = slack_adapter_with_handler_spy
    event = {"type": "message", "text": "hi", "channel": "D1", "user": "U1", "ts": "100.5"}
    await adapter._process_message_event(event, "100.5", turn_id="slack:Ev1", release_lock=lambda: None)
    context, channel = handler.handle_message.call_args.args
    assert context.metadata["turn_id"] == "slack:Ev1"
    assert context.metadata["origin_message_id"] == "100.5"
    assert context.metadata["event_time"] == 100.5


async def test_long_turn_hook_releases_the_thread_lock(slack_adapter_with_handler_spy):
    adapter, handler = slack_adapter_with_handler_spy
    released = []

    async def fake_handle(context, channel):
        await channel.on_long_turn()

    handler.handle_message = AsyncMock(side_effect=fake_handle)
    event = {"type": "message", "text": "hi", "channel": "D1", "user": "U1", "ts": "100.5"}
    await adapter._process_message_event(event, "100.5", turn_id="slack:Ev1",
                                         release_lock=lambda: released.append(True))
    assert released == [True]
