"""Guards added while typing the adapters (mypy adapters pass, 2026-10-07).

Each case is a None / BaseException / empty-payload state that used to surface as an obscure
TypeError or AttributeError deep inside the adapter, or — for the Slack signing secret — as a
check that must fail closed.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.adapters.grok_image_adapter import GrokImageAdapter, _decode_image
from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.config.environment import EnvironmentConfig
from src.domain.auth import IAMDecision
from src.domain.prompt import ANONYMOUS_ACCOUNT_ID
from src.ports.image_generation_port import ReferenceImage


# --------------------------------------------------------------------------- Slack

def _slack(config=None):
    return HTTPModeAdapter(
        app=AsyncMock(),
        config=config if config is not None else {"SLACK_SIGNING_SECRET": "s", "SLACK_BOT_TOKEN": "xoxb"},
        task_service=AsyncMock(), session_store=AsyncMock(), conversation_handler=AsyncMock(),
        iam_service=AsyncMock(), dedup_store=AsyncMock(),
    )


@pytest.mark.parametrize("config", [{"SLACK_BOT_TOKEN": "xoxb"}, {"SLACK_SIGNING_SECRET": ""}])
def test_the_adapter_cannot_be_built_without_a_signing_secret(config):
    """An empty secret would sign with b"" — so it is refused at construction, never at verify time."""
    with pytest.raises(ValueError, match="SLACK_SIGNING_SECRET"):
        _slack(config)


@pytest.mark.parametrize("method", ["_process_message_event", "_process_mention_event"])
async def test_slack_event_without_a_channel_is_skipped(method):
    adapter = _slack()

    await getattr(adapter, method)({"text": "hi", "user": "U1", "ts": "1.0"}, "sess")

    adapter.iam_service.authorize.assert_not_awaited()


@pytest.mark.parametrize("method", ["_process_message_event", "_process_mention_event"])
async def test_slack_reject_without_a_message_sends_nothing(method):
    adapter = _slack()
    adapter.iam_service.authorize.return_value = IAMDecision(action="reject", message=None)

    with patch("src.adapters.slack.http_adapter.SlackResponseChannel") as channel_cls:
        await getattr(adapter, method)({"text": "hi", "user": "U1", "channel": "C1", "ts": "1.0"}, "sess")

    channel_cls.return_value.send_message.assert_not_called()
    adapter.conversation_handler.handle_message.assert_not_awaited()


@pytest.mark.parametrize("method", ["_process_message_event", "_process_mention_event"])
async def test_slack_allow_without_a_user_is_dropped_not_crashed(method):
    adapter = _slack()
    adapter.iam_service.authorize.return_value = IAMDecision(action="allow", user=None)

    await getattr(adapter, method)({"text": "hi", "user": "U1", "channel": "C1", "ts": "1.0"}, "sess")

    adapter.conversation_handler.handle_message.assert_not_awaited()


# ------------------------------------------------------------------------ Telegram

def _telegram(iam_decision):
    adapter = object.__new__(TelegramWebhookAdapter)
    adapter.conversation_handler = AsyncMock()
    adapter.iam_service = AsyncMock()
    adapter.iam_service.authorize.return_value = iam_decision
    adapter.audio_service = None
    adapter._language_service = None
    adapter._localization = None
    adapter.bot = MagicMock()
    return adapter


def _tg_message(text):
    message = MagicMock()
    message.from_user.id = 424242
    message.chat.id = 987654
    message.text = text
    message.caption = None
    message.is_topic_message = False
    message.message_thread_id = None
    message.photo = None
    message.document = None
    message.forward_origin = None
    return message


async def test_telegram_reject_without_a_message_sends_nothing():
    adapter = _telegram(IAMDecision(action="reject", message=None))

    await adapter._process_message(_tg_message("hello"))

    adapter.conversation_handler.handle_message.assert_not_awaited()
    adapter.bot.send_message.assert_not_called()


async def test_telegram_allow_without_a_user_is_dropped_not_crashed():
    adapter = _telegram(IAMDecision(action="allow", user=None))

    await adapter._process_message(_tg_message("hello"))

    adapter.conversation_handler.handle_message.assert_not_awaited()


async def test_telegram_account_without_an_id_gets_the_anonymous_account():
    user = MagicMock(user_id="user-1", account_id=None)
    adapter = _telegram(IAMDecision(action="allow", user=user))

    await adapter._process_message(_tg_message("$consolidate"))

    context = adapter.conversation_handler.handle_command.call_args.args[1]
    assert context.account_id == ANONYMOUS_ACCOUNT_ID


# ------------------------------------------------------------------- xAI image

def test_an_image_item_without_b64_is_an_explicit_error():
    item = MagicMock(b64_json=None)
    with pytest.raises(ValueError, match="b64_json"):
        _decode_image(item)


async def test_generate_with_no_image_data_returns_empty_per_the_port_contract():
    adapter = GrokImageAdapter(api_key="fake")
    adapter._client.images.generate = AsyncMock(return_value=MagicMock(data=None))

    assert await adapter.generate("a cat") == []


async def test_edit_with_no_image_data_raises_a_clear_error():
    adapter = GrokImageAdapter(api_key="fake")
    adapter._client.post = AsyncMock(return_value=MagicMock(data=[]))

    with pytest.raises(ValueError, match="no image data"):
        await adapter.edit("recolour", [ReferenceImage(data=b"x", mime_type="image/png")])


# ------------------------------------------------------------------- realtime

@pytest.mark.parametrize("adapter_cls", [OpenAIRealtimeAdapter, XaiRealtimeAdapter])
class TestRealtimeAdapters:

    def test_using_the_socket_before_open_is_a_clear_error(self, adapter_cls):
        adapter = adapter_cls(api_key="k")
        with pytest.raises(RuntimeError, match="not open"):
            adapter._socket

    def test_an_audio_event_without_a_payload_is_dropped(self, adapter_cls):
        adapter = adapter_cls(api_key="k")
        assert adapter._normalize({"type": "response.output_audio.delta", "item_id": "i1"}) is None


# ------------------------------------------------- gather(return_exceptions=True)

async def test_a_cancelled_vector_query_does_not_break_the_rrf_merge():
    """gather(return_exceptions=True) hands back a CancelledError (a BaseException, not an Exception)."""
    env = MagicMock(spec=EnvironmentConfig)
    env.task_search_index_collection = "idx"
    collection = MagicMock()
    collection.where.return_value = collection
    cancelled, empty = MagicMock(), MagicMock()
    cancelled.get = AsyncMock(side_effect=asyncio.CancelledError())
    empty.get = AsyncMock(return_value=[])
    collection.find_nearest.side_effect = [cancelled, empty]
    db = MagicMock()
    db.collection.return_value = collection
    index = FirestoreTaskSearchIndex(db, env)

    result = await index.find_nearest("user-1", {"content": [0.1], "context": [0.2]})

    assert result == []
