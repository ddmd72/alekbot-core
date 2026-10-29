"""A `file` / `widget` rich-content item Smart returns on a background path reaches the chat as the real file
(notify_answer_copy for a call's answer, notify for reminders and reviews), not as its fallback text."""
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.messaging import RichContent, SmartResponse
from src.domain.notification import NotificationChannel
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.rich_content_service import RichContentService
from src.services.user_notification_service import UserNotificationService
from tests.unit.services.test_rich_content_deliver import FILE, FakeMedia


class FakeChannel:
    """Like SlackResponseChannel when `opens_dm`: sending a message turns a stored user id (U...) into the DM id (D...)."""

    def __init__(self, channel_id, opens_dm=True):
        self.channel_id = channel_id
        self.opens_dm = opens_dm
        self.texts, self.rich = [], []

    async def send_message(self, text, thread_id=None, **kw):
        self.texts.append(text)
        if self.opens_dm and self.channel_id.startswith("U"):
            self.channel_id = "D77"

    async def send_long_text(self, text, link_list=None, thread_id=None):
        await self.send_message(text)

    async def send_rich_content(self, content, thread_id=None):
        self.rich.append(content)


def _build(platform="slack", stored="U1", with_service=True, answer=None):
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = None
    state.get.return_value = NotificationChannel(user_id="u1", platform=platform, channel_id=stored,
                                                 updated_at=datetime(2026, 1, 1))
    channel = FakeChannel(stored, opens_dm=platform == "slack")
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    response = MagicMock(spec=AgentResponse)
    response.status = AgentStatus.SUCCESS
    response.result = answer
    coordinator = AsyncMock()
    coordinator.route_message.return_value = response
    svc = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=coordinator,
                                  notification_sla=NOTIFICATION_SLA)
    media = FakeMedia()
    if with_service:
        svc.register_platform_rich_content(platform, RichContentService(media_port=media))
    return svc, channel, media


def _answer(text="See the attached file rome_hotel.txt"):
    return SmartResponse(text=text, structured_data=RichContent(
        content_type="file", data=FILE, fallback_text="FALLBACK rome_hotel.txt"))


async def test_answer_copy_uploads_the_file_to_the_resolved_dm_channel():
    svc, channel, media = _build()
    await svc.notify_answer_copy("u1", "a1", _answer())
    assert media.files == [(FILE["content"].encode(), "rome_hotel.txt", "Rome hotel booking", "D77")]
    assert channel.rich == []


async def test_answer_copy_without_a_registered_service_keeps_the_old_path_and_warns(caplog):
    svc, channel, media = _build(with_service=False)
    with caplog.at_level(logging.WARNING):
        await svc.notify_answer_copy("u1", "a1", _answer())
    assert media.files == [] and len(channel.rich) == 1
    assert any("platform=slack" in r.getMessage() and "'file'" in r.getMessage() for r in caplog.records)


async def test_notify_uploads_the_file_from_the_rich_content_branch():
    svc, channel, media = _build(answer=_answer("Here is your booking."))
    result = await svc.notify("u1", "a1", "alert", kind=NotificationKind.REMINDER)
    assert result.delivered
    assert [f[1:] for f in media.files] == [("rome_hotel.txt", "Rome hotel booking", "D77")]
    assert channel.rich == []


async def test_notify_with_no_text_opens_the_dm_before_the_upload():
    svc, channel, media = _build(answer=_answer(""))
    await svc.notify("u1", "a1", "alert", kind=NotificationKind.REMINDER)
    assert channel.texts == ["📎"]
    assert media.files[0][3] == "D77"


async def test_notify_without_a_registered_service_sends_the_fallback_through_the_renderer():
    svc, channel, media = _build(with_service=False, answer=_answer("Here is your booking."))
    await svc.notify("u1", "a1", "alert", kind=NotificationKind.REMINDER)
    assert media.files == [] and len(channel.rich) == 1


async def test_telegram_chat_id_is_used_as_it_is():
    svc, channel, media = _build(platform="telegram", stored="12345")
    await svc.notify_answer_copy("u1", "a1", _answer())
    assert media.files[0][3] == "12345"
