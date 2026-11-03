"""notify_document_link(): a failed delivery is swallowed but logged as an error."""
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.notification import NotificationChannel
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService


@pytest.mark.asyncio
async def test_document_link_delivery_failure_is_logged_not_raised(caplog):
    channel = MagicMock()
    channel.send_document_link = AsyncMock(side_effect=RuntimeError("boom-link"))
    state = AsyncMock(spec=NotificationStatePort)
    state.get.return_value = NotificationChannel(
        user_id="user-mut-1", platform="telegram", channel_id="C-mut-1", updated_at=datetime(2026, 1, 1),
    )
    state.get_primary.return_value = None
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    service = UserNotificationService(
        state_repo=state, channel_factory=factory, coordinator=AsyncMock(),
        notification_sla=NOTIFICATION_SLA, session_store=AsyncMock(),
    )

    with caplog.at_level(logging.ERROR):
        await service.notify_document_link(
            user_id="user-mut-1", account_id="account-mut-1", url="https://x.example/f/t", label="Doc",
        )

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Document link delivery failed" in m and "boom-link" in m for m in errors)
