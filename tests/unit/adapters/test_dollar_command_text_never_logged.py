"""Neither chat adapter logs the text of a `$` message (CABINET_DEVICE_SESSIONS_RFC §7.9): a `$<digits>` message may be
the facts PIN or the duress code. One contract on both adapters: the message goes in with every log level captured,
reaches the conversation handler as a command, and no log record carries its text. An ordinary message's text is
still previewed where it was before (the Slack file line), so the guard is the `$`, not a lost log line."""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.language import LanguageCode
from src.domain.user import UserProfile
from src.ports.platform_auth_port import IAMDecision

ALEX = UserProfile(user_id="user-alex", email="alex@example.com", account_id="acct-alex")
CODE = "$0001"
FILE = {"url_private": "https://files.example.test/scan.png", "name": "scan.png", "mimetype": "image/png", "size": 10}


def _iam():
    iam = AsyncMock()
    iam.authorize.return_value = IAMDecision(action="allow", user=ALEX)
    return iam


async def _slack(text: str, handler) -> None:
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = handler
    adapter.iam_service = _iam()
    adapter.app = MagicMock()
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._resolve_language = AsyncMock(return_value=LanguageCode.EN)
    event = {"type": "message", "subtype": "file_share", "text": text, "channel": "D1", "user": "U-alex",
             "ts": "1.1", "channel_type": "im", "files": [FILE]}
    await adapter._process_message_event(event, "1.1")


async def _telegram(text: str, handler) -> None:
    with patch("src.adapters.telegram.webhook_adapter.Bot"):
        adapter = TelegramWebhookAdapter(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                                         webhook_secret="test_webhook_secret_32chars_min", dedup_store=AsyncMock(),
                                         session_store=AsyncMock(), conversation_handler=handler, iam_service=_iam())
    message = MagicMock()
    message.from_user.id = 7001
    message.chat.id = 7001
    message.text, message.caption = text, None
    message.location = None
    message.forward_origin = None
    message.is_topic_message = False
    await adapter._process_message(message, update_id=1)


ADAPTERS = {"slack": _slack, "telegram": _telegram}


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
@pytest.mark.parametrize("text", [CODE, "`$0001`", "$0002 "])
async def test_a_dollar_message_reaches_the_handler_and_no_log_record_carries_its_text(adapter, text, caplog):
    handler = AsyncMock()

    with caplog.at_level(logging.DEBUG):
        await ADAPTERS[adapter](text, handler)

    handler.handle_command.assert_awaited_once()
    assert handler.handle_command.call_args.args[0] == text.strip("` ").lstrip("$")
    digits = text.strip("` $")
    assert [r.getMessage() for r in caplog.records if digits in r.getMessage()] == []


@pytest.mark.parametrize("adapter", sorted(ADAPTERS))
async def test_an_ordinary_message_is_still_previewed(adapter, caplog):
    handler = AsyncMock()

    with caplog.at_level(logging.DEBUG):
        await ADAPTERS[adapter]("look at 12 Oak Street", handler)

    assert any("look at 12 Oak Street" in r.getMessage() for r in caplog.records)
