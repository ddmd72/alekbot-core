"""`notify_raw(literal=True)` (S4 review A4/R1): text the bot did not write goes through the channel's literal send, never
through the markdown path; the default is unchanged."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.literal_notice import render_literal_notice
from src.domain.notification import NotificationChannel
from src.domain.ui_messages import UIMessage
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from tests.support.fact_review_world import FakeNotificationState, RecordingChannelFactory


def _service(literal_error=None):
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = None
    state.get.return_value = NotificationChannel(user_id="u", platform="slack", channel_id="C1",
                                                 updated_at=datetime(2026, 1, 1))
    channel = MagicMock()
    channel.channel_id = "D1"
    channel.send_long_text = AsyncMock()
    channel.send_literal = AsyncMock(side_effect=literal_error)
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    service = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=MagicMock(),
                                      notification_sla={})
    return service, channel


async def test_a_literal_notice_uses_the_literal_send_only():
    service, channel = _service()
    assert await service.notify_raw("u", "a", "• «Wipe <!channel>» — archived", literal=True) is True
    channel.send_literal.assert_awaited_once_with("• «Wipe <!channel>» — archived")
    channel.send_long_text.assert_not_awaited()


async def test_the_default_is_unchanged():
    service, channel = _service()
    assert await service.notify_raw("u", "a", "hello") is True
    channel.send_long_text.assert_awaited_once_with("hello")
    channel.send_literal.assert_not_awaited()


async def test_a_failed_literal_send_is_swallowed_and_reported_as_not_sent():
    service, _ = _service(literal_error=NotImplementedError("web cannot send literal text"))
    assert await service.notify_raw("u", "a", "hello", literal=True) is False


# ------------------------------------------------- the account lock's notice to every stored channel (RFC §7.8)

_T0 = datetime(2026, 11, 1, 9, 0)
_LOCKED = UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE


class _OneChannelFails(RecordingChannelFactory):
    def __init__(self, failing) -> None:
        super().__init__()
        self.failing = failing

    def create(self, platform, channel_id):
        channel = super().create(platform, channel_id)
        if channel is not None and (platform, channel_id) == self.failing:
            channel.fail = True
        return channel


class _BrokenPrimary(FakeNotificationState):
    async def get_primary(self, user_id):
        raise RuntimeError("store down")


def _all_channels(state=None, factory=None, localization=True):
    state = state or FakeNotificationState(_T0)
    factory = factory or RecordingChannelFactory()
    service = UserNotificationService(state, factory, coordinator=MagicMock(), notification_sla={},
                                      localization=LocalizationService(FileLocalizationAdapter()) if localization
                                      else None)
    return service, state, factory


async def test_the_all_channels_notice_reaches_the_primary_and_the_last_active_channel_literally():
    service, state, factory = _all_channels()
    await state.save_primary("u", "slack", "C1")
    await state.save("u", "telegram", "chat-1")
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 2
    texts = [(p, c) for p, c, _ in factory.sent_literal]
    assert texts == [("slack", "C1"), ("telegram", "chat-1")] and factory.sent == factory.sent_literal
    assert all("administrator" in text for _, _, text in factory.sent_literal)


async def test_one_channel_that_is_primary_and_last_active_gets_it_once():
    service, state, factory = _all_channels()
    await state.save_primary("u", "telegram", "chat-1")
    await state.save("u", "telegram", "chat-1")
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 1
    assert len(factory.sent_literal) == 1


async def test_a_failing_or_missing_channel_does_not_stop_the_other():
    service, state, factory = _all_channels(factory=_OneChannelFails(("slack", "C1")))
    await state.save_primary("u", "slack", "C1")
    await state.save("u", "telegram", "chat-1")
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 1
    assert [(p, c) for p, c, _ in factory.sent_literal] == [("telegram", "chat-1")]

    factory.unconfigured.add("telegram")
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 0


async def test_a_failing_channel_read_does_not_stop_the_other(caplog):
    service, state, factory = _all_channels(state=_BrokenPrimary(_T0))
    await state.save("u", "telegram", "chat-1")
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 1
    assert "primary channel read failed" in caplog.text


async def test_no_stored_channel_or_no_localization_sends_nothing(caplog):
    service, _, factory = _all_channels()
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 0
    service, state, factory = _all_channels(localization=False)
    await state.save("u", "telegram", "chat-1")
    assert await service.notify_ui_notice_all_channels("u", _LOCKED) == 0
    assert factory.sent == [] and "No localization wired" in caplog.text


# ------------------------------------------------- a literal notice's file (domain.literal_notice), before its text

_NOTICE = render_literal_notice("📌 Reminder set: \"Morning briefing\"", "Follow the workspace file.",
                                filename="reminder.txt", title="Morning briefing")


async def test_an_attachment_posts_the_file_first_then_the_text_literally():
    service, channel = _service()
    order = []
    channel.send_file = AsyncMock(side_effect=lambda **kw: order.append(("file", kw)))
    channel.send_literal.side_effect = lambda text: order.append(("literal", text))

    assert await service.notify_raw("u", "a", _NOTICE.message, literal=True, attachment=_NOTICE) is True

    assert order == [
        ("file", {"content": b"Follow the workspace file.", "filename": "reminder.txt", "title": "Morning briefing"}),
        ("literal", _NOTICE.message),
    ]


async def test_a_failed_file_sends_no_text_and_reports_not_sent():
    service, channel = _service()
    channel.send_file = AsyncMock(side_effect=RuntimeError("slack down"))

    assert await service.notify_raw("u", "a", _NOTICE.message, literal=True, attachment=_NOTICE) is False
    channel.send_literal.assert_not_awaited()
