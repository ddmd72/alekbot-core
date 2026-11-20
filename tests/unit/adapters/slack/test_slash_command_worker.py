"""A queued slash command runs as the `$` command of the same name, always in the bot's DM with the user."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.language import LanguageCode


def _make_adapter(conversation_handler, iam_service, *, dm_id="D0DM", open_error=None):
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = conversation_handler
    adapter.iam_service = iam_service
    adapter.app = MagicMock()
    adapter.app.client.conversations_open = AsyncMock(
        side_effect=open_error, return_value={"channel": {"id": dm_id}})
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._session_locks = {}
    adapter._resolve_language = AsyncMock(return_value=LanguageCode.EN)
    return adapter


def _allowing_iam():
    iam = AsyncMock()
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="acct-1")
    iam.authorize.return_value = decision
    return iam


def _event(command="/geo", text="Look Here", channel="D0HUMANS"):
    return {"type": "slash_command", "command": command, "text": text, "user": "U0SLACK", "channel": channel}


async def test_a_slash_command_runs_as_the_dollar_command_in_the_dm():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_slash_command(_event(), "U0SLACK")

    adapter.app.client.conversations_open.assert_awaited_once_with(users="U0SLACK")
    handler.handle_message.assert_not_called()
    handler.handle_command.assert_awaited_once()
    command, ctx, _channel = handler.handle_command.call_args.args
    assert command == "geo look here"
    assert ctx.text == "$geo Look Here"
    assert ctx.metadata["channel"] == "D0DM"
    assert ctx.metadata["channel_type"] == "im"
    assert ctx.metadata["platform"] == "slack"


async def test_a_slash_command_from_a_channel_still_lands_in_the_dm():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_slash_command(_event(channel="C0CHAN"), "U0SLACK")

    ctx = handler.handle_command.call_args.args[1]
    assert ctx.metadata["channel"] == "D0DM"
    assert ctx.metadata["channel_type"] == "im"


async def test_a_slash_command_without_text_is_the_bare_command():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_slash_command(_event(text=""), "U0SLACK")

    assert handler.handle_command.call_args.args[0] == "geo"


async def test_any_command_name_needs_no_code():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_slash_command(_event(command="/memory", text="save"), "U0SLACK")

    assert handler.handle_command.call_args.args[0] == "memory save"


async def test_a_rejected_user_gets_the_reject_message_in_the_dm_and_no_command():
    handler = AsyncMock()
    iam = AsyncMock()
    decision = MagicMock()
    decision.action = "reject"
    decision.message = "Not allowed"
    iam.authorize.return_value = decision
    adapter = _make_adapter(handler, iam)
    adapter.app.client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1.1"})

    await adapter._process_slash_command(_event(channel="C0CHAN"), "U0SLACK")

    handler.handle_command.assert_not_called()
    handler.handle_message.assert_not_called()
    adapter.app.client.chat_postMessage.assert_awaited()
    assert adapter.app.client.chat_postMessage.call_args.kwargs["channel"] == "D0DM"


async def test_a_failing_conversations_open_dispatches_nothing_and_does_not_raise():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam(), open_error=RuntimeError("slack down"))

    await adapter._process_slash_command(_event(), "U0SLACK")

    handler.handle_command.assert_not_called()
    handler.handle_message.assert_not_called()


def test_a_slash_command_with_a_user_is_not_ignored_before_the_lock():
    assert HTTPModeAdapter._ignored_before_lock(_event()) is False


def test_a_slash_command_without_a_user_is_ignored_before_the_lock():
    event = _event()
    del event["user"]
    assert HTTPModeAdapter._ignored_before_lock(event) is True


async def test_handle_queued_event_routes_a_slash_command():
    adapter = _make_adapter(AsyncMock(), _allowing_iam())
    adapter._process_slash_command = AsyncMock()

    body, status = await adapter.handle_queued_event(
        {"event": {"event_id": None, "event": _event()}, "session_id": "U0SLACK"}, {})

    assert status == 200
    adapter._process_slash_command.assert_awaited_once()
    assert adapter._process_slash_command.call_args.args[0]["command"] == "/geo"
