"""The adapters pass only the UI language: no per-turn `preferred_language` / `agent_mirror` metadata (nothing read it);
the response language comes only from the USER override."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import _message_metadata
from src.agents.base_agent import BaseAgent
from src.domain.language import LanguageCode

_KEYS = ("preferred_language", "agent_mirror")


def _adapter(conversation_handler):
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = conversation_handler
    iam_service = AsyncMock()
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="u1", account_id="acc-1")
    iam_service.authorize.return_value = decision
    adapter.iam_service = iam_service
    adapter.app = MagicMock()
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._resolve_language = AsyncMock(return_value=LanguageCode.EN)
    return adapter


async def test_a_slack_message_turn_carries_no_language_metadata():
    handler = AsyncMock()
    event = {"text": "2 + 2", "channel": "D0CHAN", "user": "U0SLACK", "ts": "1760000200.000200"}

    await _adapter(handler)._process_message_event(event, session_id="ignored")

    context = handler.handle_message.call_args.args[0]
    assert context.language == "en"
    assert not any(key in context.metadata for key in _KEYS)


async def test_a_slack_mention_carries_no_language_metadata():
    handler = AsyncMock()
    event = {"text": "<@U0BOT> 2 + 2", "channel": "C0CHAN", "user": "U0SLACK", "ts": "1760000200.000200"}

    await _adapter(handler)._process_mention_event(event, session_id="ignored")

    context = handler.handle_message.call_args.args[0]
    assert context.language == "en"
    assert not any(key in context.metadata for key in _KEYS)


def test_a_telegram_turn_carries_no_language_metadata():
    message = SimpleNamespace(message_id=1, date=None)

    metadata = _message_metadata(message, chat_id="C0CHAN", telegram_user_id="u1", update_id=1)

    assert not any(key in metadata for key in _KEYS)


def test_no_agent_declares_forwards_language_preference():
    assert not hasattr(BaseAgent, "forwards_language_preference")
