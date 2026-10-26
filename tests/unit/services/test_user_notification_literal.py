"""`notify_raw(literal=True)` (S4 review A4/R1): text the bot did not write goes through the channel's literal send, never
through the markdown path; the default is unchanged."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.domain.notification import NotificationChannel
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService


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
