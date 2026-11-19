"""Telegram location pin (GEO_MARKS_RFC §4.6): a plain pin is a geo mark, a venue / live / forwarded one is not."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Location, Venue

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.geo_mark import GeoFix, GeoSource
from src.domain.messaging import MessageContext
from src.domain.ui_messages import UIMessage
from src.domain.user import UserProfile
from src.locales.uk import UI_STRINGS
from src.ports.platform_auth_port import IAMDecision

SHARED_AT = datetime(2026, 11, 19, 10, 30, tzinfo=timezone.utc)


class FakeHandler:
    """Records what the adapter hands over; nothing else."""

    def __init__(self):
        self.marks = []
        self.messages = []

    async def handle_geo_mark(self, fix, context, response_channel):
        self.marks.append((fix, context, response_channel))

    async def handle_message(self, context, response_channel):
        self.messages.append(context)

    async def handle_command(self, command, context, response_channel):  # pragma: no cover - never reached here
        raise AssertionError("a pin is not a command")


def _message(location=None, venue=None, forward_origin=None, text=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=100), text=text, caption=None,
        photo=None, document=None, voice=None, audio=None, is_topic_message=False, message_thread_id=None,
        message_id=55, date=SHARED_AT, reply_to_message=None, location=location, venue=venue,
        forward_origin=forward_origin,
    )


def _pin(accuracy=40.0, live_period=None):
    return Location(latitude=41.1, longitude=-8.6, horizontal_accuracy=accuracy, live_period=live_period)


@pytest.fixture
def adapter():
    with patch("src.adapters.telegram.webhook_adapter.Bot") as bot_cls:
        bot_cls.return_value = AsyncMock()
        instance = TelegramWebhookAdapter(
            token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            webhook_secret="test_webhook_secret_32chars_min",
            dedup_store=AsyncMock(), session_store=AsyncMock(),
            conversation_handler=FakeHandler(), iam_service=AsyncMock(),
        )
    instance.iam_service.authorize.return_value = IAMDecision(
        action="allow", user=UserProfile(user_id="usr-1", email="alex@example.com", account_id="acc-1"))
    return instance


async def test_pin_with_accuracy_becomes_one_geo_mark(adapter):
    await adapter._process_message(_message(location=_pin(40.0)), update_id=9)

    assert len(adapter.conversation_handler.marks) == 1
    fix, context, _ = adapter.conversation_handler.marks[0]
    assert isinstance(fix, GeoFix)
    assert (fix.latitude, fix.longitude, fix.accuracy_m) == (41.1, -8.6, 40.0)
    assert fix.shared_at == SHARED_AT
    assert fix.source is GeoSource.TELEGRAM_PIN
    assert isinstance(context, MessageContext)
    assert (context.user_id, context.account_id) == ("usr-1", "acc-1")
    assert context.session_id == "usr-1:100"
    assert context.metadata["channel"] == "100"
    assert context.metadata["turn_id"] == "telegram:9"
    assert context.metadata["origin_message_id"] == "55"
    assert adapter.conversation_handler.messages == []


async def test_pin_without_accuracy_has_none(adapter):
    await adapter._process_message(_message(location=_pin(None)))

    fix = adapter.conversation_handler.marks[0][0]
    assert fix.accuracy_m is None
    assert adapter.conversation_handler.marks[0][1].metadata["turn_id"] is None


async def test_venue_is_not_a_mark_and_gets_the_venue_line(adapter):
    venue = Venue(location=_pin(), title="Iron Temple gym", address="12 Oak Street, Springfield")

    await adapter._process_message(_message(location=venue.location, venue=venue))

    assert adapter.conversation_handler.marks == []
    adapter.bot.send_message.assert_awaited_once()
    assert adapter.bot.send_message.call_args.kwargs["text"].replace("\\", "") == UI_STRINGS[UIMessage.GEO_TELEGRAM_VENUE.value]


async def test_live_location_is_not_a_mark_and_gets_the_live_line(adapter):
    await adapter._process_message(_message(location=_pin(live_period=900)))

    assert adapter.conversation_handler.marks == []
    adapter.bot.send_message.assert_awaited_once()
    assert adapter.bot.send_message.call_args.kwargs["text"].replace("\\", "") == UI_STRINGS[UIMessage.GEO_TELEGRAM_LIVE.value]


async def test_notice_uses_the_wired_localization(adapter):
    adapter._localization = MagicMock()
    adapter._localization.get_ui_string.return_value = "live line"

    await adapter._process_message(_message(location=_pin(live_period=900)))

    assert adapter.bot.send_message.call_args.kwargs["text"] == "live line"
    assert adapter._localization.get_ui_string.call_args.args[1] is UIMessage.GEO_TELEGRAM_LIVE


async def test_forwarded_pin_is_not_a_mark(adapter):
    await adapter._process_message(_message(location=_pin(), forward_origin=object()))

    assert adapter.conversation_handler.marks == []
    adapter.bot.send_message.assert_not_awaited()


async def test_unauthorized_user_gets_the_iam_message_and_no_mark(adapter):
    adapter.iam_service.authorize.return_value = IAMDecision(action="reject", message="no access")

    await adapter._process_message(_message(location=_pin()))

    assert adapter.conversation_handler.marks == []
    assert adapter.bot.send_message.call_args.kwargs["text"] == "no access"


async def test_malformed_coordinate_is_dropped_silently(adapter):
    bad = Location(latitude=95.0, longitude=-8.6)

    await adapter._process_message(_message(location=bad))

    assert adapter.conversation_handler.marks == []
    adapter.bot.send_message.assert_not_awaited()


async def test_queued_update_handles_a_pin_the_same_way(adapter):
    update = SimpleNamespace(update_id=12, message=_message(location=_pin(5.0)))

    with patch("src.adapters.telegram.webhook_adapter.Update") as update_cls:
        update_cls.de_json.return_value = update
        result = await adapter.handle_queued_update({"update": {"update_id": 12}})

    assert result == ({"ok": True}, 200)
    fix, context, _ = adapter.conversation_handler.marks[0]
    assert fix.accuracy_m == 5.0
    assert context.metadata["turn_id"] == "telegram:12"
    assert adapter.conversation_handler.messages == []


async def test_text_message_still_goes_to_handle_message(adapter):
    await adapter._process_message(_message(text="hello"))

    assert adapter.conversation_handler.marks == []
    assert [c.text for c in adapter.conversation_handler.messages] == ["hello"]
