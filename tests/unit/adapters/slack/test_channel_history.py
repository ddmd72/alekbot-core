"""
Unit tests for SlackChannelHistorySource's `$command` filtering.

A command echoed back into the channel (plain, or backtick-wrapped — e.g. the bot's own
`` `$skill save CODE` `` copy-paste prompt) must never be treated as conversation history.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.channel_history import SlackChannelHistorySource

_BOT_USER_ID = "U-BOT"


def _make_source(messages) -> SlackChannelHistorySource:
    client = MagicMock()
    client.conversations_history = AsyncMock(return_value={"messages": messages})
    return SlackChannelHistorySource(client, _BOT_USER_ID)


@pytest.mark.asyncio
async def test_plain_dollar_command_excluded_from_history():
    messages = [
        {"text": "ignored current input", "ts": "3", "user": "U1"},
        {"text": "$skill list", "ts": "2", "user": "U1"},
        {"text": "hello there", "ts": "1", "user": "U1"},
    ]
    source = _make_source(messages)

    result = await source.fetch("C1", limit=10)

    assert [m.parts[0].text for m in result] == ["hello there"]


@pytest.mark.asyncio
async def test_backtick_wrapped_dollar_command_excluded_from_history():
    """The bot's own `` `$skill save 7f3a` `` echo must be filtered like a plain command."""
    messages = [
        {"text": "ignored current input", "ts": "3", "user": "U1"},
        {"text": "`$skill save 7f3a`", "ts": "2", "user": _BOT_USER_ID, "bot_id": "B1"},
        {"text": "hello there", "ts": "1", "user": "U1"},
    ]
    source = _make_source(messages)

    result = await source.fetch("C1", limit=10)

    assert [m.parts[0].text for m in result] == ["hello there"]


@pytest.mark.asyncio
async def test_backticks_around_a_substring_stay_in_history():
    """"`$HOME` is wrong" — backticks around a substring, not the whole text — is kept."""
    messages = [
        {"text": "ignored current input", "ts": "2", "user": "U1"},
        {"text": "`$HOME` is wrong", "ts": "1", "user": "U1"},
    ]
    source = _make_source(messages)

    result = await source.fetch("C1", limit=10)

    assert [m.parts[0].text for m in result] == ["`$HOME` is wrong"]
