"""Telegram webhook adapter (Facts S4 · T5b): a message that replies to another one carries that message's id as
`reply_to_message_id`. The updates are built by the SDK's own `Update.de_json` from a webhook payload."""
from unittest.mock import AsyncMock, MagicMock

from telegram import Bot, Update

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter


def _adapter(conversation_handler):
    adapter = object.__new__(TelegramWebhookAdapter)
    adapter.conversation_handler = conversation_handler
    iam_service = AsyncMock()
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    iam_service.authorize.return_value = decision
    adapter.iam_service = iam_service
    adapter.audio_service = None
    adapter._language_service = None
    adapter._localization = None
    adapter.bot = MagicMock()
    return adapter


def _payload(**message_fields):
    message = {
        "message_id": 20, "date": 1760000000,
        "chat": {"id": 987654, "type": "private"},
        "from": {"id": 424242, "is_bot": False, "first_name": "Owner"},
        "text": "2",
    }
    message.update(message_fields)
    return {"update_id": 9001, "message": message}


async def _metadata(message):
    handler = AsyncMock()
    await _adapter(handler)._process_message(message)
    handler.handle_message.assert_awaited_once()
    return handler.handle_message.call_args.args[0].metadata


async def test_a_reply_carries_the_id_of_the_message_it_replies_to():
    replied = {"message_id": 17, "date": 1759999000, "chat": {"id": 987654, "type": "private"},
               "from": {"id": 1, "is_bot": True, "first_name": "Alek"}, "text": "Is this still true?"}
    update = Update.de_json(_payload(reply_to_message=replied), Bot("123456:TEST"))
    assert (await _metadata(update.message))["reply_to_message_id"] == "17"


async def test_a_message_that_replies_to_nothing_carries_no_reply_to():
    update = Update.de_json(_payload(), Bot("123456:TEST"))
    assert "reply_to_message_id" not in await _metadata(update.message)


async def test_a_mock_message_gives_no_reply_to_message_id():
    message = MagicMock()
    message.from_user.id = 424242
    message.chat.id = 987654
    message.text = "hello"
    message.caption = None
    message.is_topic_message = False
    message.message_thread_id = None
    message.photo = None
    message.document = None
    message.voice = None
    message.audio = None
    message.forward_origin = None
    assert "reply_to_message_id" not in await _metadata(message)
