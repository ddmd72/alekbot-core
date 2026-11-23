"""An unknown command that is mostly digits (a mistyped PIN such as `$1234.` or `$12 34`) is never echoed."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.command_services import CommandServices
from src.handlers.conversation_handler import ConversationHandler
from src.services.localization_service import LocalizationService
from tests.support.pin_lock_world import en


async def _reply(command: str) -> str:
    handler = ConversationHandler(
        coordinator=MagicMock(), agent_factory=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        localization=LocalizationService(FileLocalizationAdapter()), commands=CommandServices())
    channel = MagicMock()
    channel.send_message = AsyncMock()
    context = MessageContext(text=f"${command}", session_id="user-alex:C-chat", user_id="user-alex",
                             account_id="acct-alex", language="en", attachments=[],
                             metadata={"platform": "slack", "channel": "C-chat"})
    await handler.handle_command(command, context, channel)
    return channel.send_message.await_args_list[0].args[0]


@pytest.mark.parametrize("command", ["1234.", "12 34", "12-34", "4071x"])
async def test_a_near_digit_command_is_not_echoed(command):
    reply = await _reply(command)
    assert "0001" not in reply and "1234" not in reply and "12" not in reply
    assert reply == en(UIMessage.UNKNOWN_COMMAND, command="…")


async def test_a_word_command_is_still_echoed():
    assert await _reply("geography") == en(UIMessage.UNKNOWN_COMMAND, command="geography")
