"""`UserNotificationService.notify` posts the show-only notice of an external task its background run created (owner's
decision 2026-11-24), and keeps out of history the alert text a caller asks it to (`system_alert_history`).

The real service over a recording channel and a coordinator stub that answers like Smart: a text and the receipt item
of a queued task. Assertions are on what reached the channel, in order, and on the history pair. The real-stack path
(a reminder's run through Smart and the task specialist) is `tests/unit/infrastructure/test_external_task_reminder_notice.py`.
Alex Doe is `usr-1`; the worker is `research`.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import AgentResponse, DeliveryItem
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from src.domain.external_task_views import CreatedTask
from src.domain.messaging import SmartResponse
from src.domain.notification import NotificationChannel
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from tests.support.external_task_world import NOTE, TEXT, TITLE

ANSWER = "The irrigation comparison went to research."
QUEUED = receipt_data(CreatedTask(task_id="t1", status=ExternalTaskStatus.QUEUED, label="research", title=TITLE,
                                  text=TEXT, note=NOTE))


class Channel:
    def __init__(self, broken_file: bool = False) -> None:
        self.posts = []
        self.broken_file = broken_file

    async def send_long_text(self, text: str, link_list=None) -> None:
        self.posts.append(("text", text))

    async def send_file(self, content: bytes, filename: str, title: str, thread_id=None) -> None:
        if self.broken_file:
            raise RuntimeError("upload failed")
        self.posts.append(("file", filename))

    async def send_literal(self, text: str, thread_id=None) -> None:
        self.posts.append(("literal", text))


class Bot:
    def __init__(self, text: str, items) -> None:
        self.text, self.items = text, items

    async def route_message(self, message) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=message.recipient,
                                     result=SmartResponse(text=self.text), delivery_items=self.items)


def _service(bot: Bot, channel: Channel, *, localized: bool = True, sessions=None) -> UserNotificationService:
    state = AsyncMock()
    state.get_primary.return_value = NotificationChannel(user_id="usr-1", platform="slack", channel_id="C-garden",
                                                         updated_at=datetime(2026, 11, 23, tzinfo=timezone.utc))
    factory = MagicMock()
    factory.create.return_value = channel
    return UserNotificationService(
        state_repo=state, channel_factory=factory, coordinator=bot, notification_sla=NOTIFICATION_SLA,
        session_store=sessions, localization=LocalizationService(FileLocalizationAdapter()) if localized else None)


async def _notify(service: UserNotificationService, **kw):
    return await service.notify("usr-1", "acc-1", "alert", kind=NotificationKind.REMINDER, **kw)


async def test_the_notice_follows_the_answer_and_other_items_are_left_alone():
    channel = Channel()
    other = DeliveryItem(type="document", data={"filename": "x.pdf"})
    result = await _notify(_service(Bot(ANSWER, [other, DeliveryItem(EXTERNAL_TASK_RECEIPT_DELIVERY, QUEUED)]), channel))
    assert result.delivered and result.sent
    assert [p[0] for p in channel.posts] == ["text", "file", "literal"] and channel.posts[1] == ("file", "task-t1.txt")


async def test_a_notice_alone_counts_as_sent():
    channel = Channel()
    result = await _notify(_service(Bot("", [DeliveryItem(EXTERNAL_TASK_RECEIPT_DELIVERY, QUEUED)]), channel))
    assert result.sent and [p[0] for p in channel.posts] == ["file", "literal"]


async def test_without_localization_or_when_the_file_fails_no_notice_is_posted_and_the_turn_stands():
    unlocalized = Channel()
    result = await _notify(_service(Bot("", [DeliveryItem(EXTERNAL_TASK_RECEIPT_DELIVERY, QUEUED)]), unlocalized,
                                    localized=False))
    assert result.delivered and not result.sent and unlocalized.posts == []
    broken = Channel(broken_file=True)
    result = await _notify(_service(Bot(ANSWER, [DeliveryItem(EXTERNAL_TASK_RECEIPT_DELIVERY, QUEUED)]), broken))
    assert result.delivered and result.sent and broken.posts == [("text", ANSWER)]


async def test_history_keeps_the_alert_a_caller_names_in_place_of_the_turns_alert():
    sessions = MagicMock()
    sessions.append_messages_batch = AsyncMock()
    await _notify(_service(Bot(ANSWER, []), Channel(), sessions=sessions),
                  system_alert_summary="label", system_alert_history="alert without the note")
    user, _ = sessions.append_messages_batch.await_args.kwargs["messages"]
    assert (user.parts[0].text, user.parts[0].full_text) == ("label", "alert without the note")
