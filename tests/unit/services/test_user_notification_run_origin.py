"""notify() tells the agent who started the run (AGENT_WORKSPACE_RFC §9): run_origin = background:<origin_detail>,
else background:<kind>; run_fire_key for a reminder fire. A fresh context per call: nothing leaks from the caller."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.messaging import SmartResponse
from src.domain.notification import NotificationChannel
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService

F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"


def _service():
    state = AsyncMock(spec=NotificationStatePort)
    state.get.return_value = NotificationChannel(user_id="u1", platform="slack", channel_id="C1",
                                                 updated_at=datetime(2026, 1, 1))
    state.get_primary.return_value = None
    channel = MagicMock()
    channel.send_long_text = AsyncMock(return_value={"ts": "1", "channel": "C1"})
    channel.send_rich_content = AsyncMock()
    channel.max_message_length = 4000
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    response = MagicMock(spec=AgentResponse)
    response.status = AgentStatus.SUCCESS
    response.result = SmartResponse(text="ok")
    coordinator = AsyncMock()
    coordinator.route_message.return_value = response
    service = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=coordinator,
                                      notification_sla=NOTIFICATION_SLA)
    return service, coordinator


def _context(coordinator):
    return coordinator.route_message.call_args[0][0].context


async def test_reminder_fire_sets_origin_and_fire_key():
    service, coordinator = _service()
    await service.notify("u1", "a1", "alert", kind=NotificationKind.REMINDER,
                         origin_detail="execute_reminder", fire_key=F)
    context = _context(coordinator)
    assert context["run_origin"] == "background:execute_reminder"
    assert context["run_fire_key"] == F


async def test_daily_review_has_no_fire_key():
    service, coordinator = _service()
    await service.notify("u1", "a1", "alert", kind=NotificationKind.DAILY_DIGEST, origin_detail="daily_email_review")
    context = _context(coordinator)
    assert context["run_origin"] == "background:daily_email_review"
    assert "run_fire_key" not in context


async def test_falls_back_to_the_kind():
    service, coordinator = _service()
    await service.notify("u1", "a1", "alert", kind=NotificationKind.DOCUMENT_DELIVERY)
    assert _context(coordinator)["run_origin"] == "background:document_delivery"
