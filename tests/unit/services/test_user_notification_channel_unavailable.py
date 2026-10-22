"""UserNotificationService: a stored channel the factory cannot build is a quiet skip, never a send or a history write.

`notify_call_summary` and `notify_answer_copy` resolve the channel first; when `ChannelFactory.create` returns None
(a platform with no adapter wired) they log a warning and return. Nothing may be delivered, nothing appended to
session history, and the caller must not see an exception (both are fire-and-forget background deliveries)."""
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.messaging import SmartResponse
from src.domain.notification import NotificationChannel
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService

_USER_ID = "user-abc123"
_ACCOUNT_ID = "account-xyz"
_CALL_EVENT = "[System: phone call with Lelik, 22:07-22:09 (2 min). The note below was posted to the user's chat.]"


@pytest.fixture
def state_repo() -> AsyncMock:
    repo = AsyncMock(spec=NotificationStatePort)
    repo.get.return_value = NotificationChannel(
        user_id=_USER_ID, platform="telegram", channel_id="C0123456", updated_at=datetime(2026, 1, 1),
    )
    repo.get_primary.return_value = None
    return repo


@pytest.fixture
def channel_factory() -> MagicMock:
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = None  # the platform has no channel adapter
    return factory


@pytest.fixture
def session_store() -> AsyncMock:
    store = AsyncMock()
    store.append_messages_batch = AsyncMock()
    return store


@pytest.fixture
def service(state_repo, channel_factory, session_store) -> UserNotificationService:
    return UserNotificationService(
        state_repo=state_repo,
        channel_factory=channel_factory,
        coordinator=AsyncMock(),
        notification_sla={},
        session_store=session_store,
    )


class TestCallSummaryWithoutAChannel:
    async def test_nothing_is_appended_to_history_and_the_platform_is_named_in_a_warning(
        self, service, channel_factory, session_store, caplog,
    ):
        with caplog.at_level(logging.WARNING):
            result = await service.notify_call_summary(
                user_id=_USER_ID, account_id=_ACCOUNT_ID, summary="Talked about Q3.", call_event=_CALL_EVENT,
            )

        assert result is None
        channel_factory.create.assert_called_once_with(platform="telegram", channel_id="C0123456")
        session_store.append_messages_batch.assert_not_awaited()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Cannot create channel for call summary" in w and "telegram" in w for w in warnings)
        # a skip, not a delivery attempt that blew up on the missing channel and was swallowed
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestAnswerCopyWithoutAChannel:
    async def test_nothing_is_delivered_or_stored_and_the_platform_is_named_in_a_warning(
        self, service, channel_factory, session_store, caplog,
    ):
        answer = SmartResponse(
            text="Your flights [1]", structured_data=MagicMock(), link_list=[{"anchor": 1, "title": "x", "url": "https://x"}],
        )

        with caplog.at_level(logging.WARNING):
            result = await service.notify_answer_copy(_USER_ID, _ACCOUNT_ID, answer)

        assert result is None
        channel_factory.create.assert_called_once_with(platform="telegram", channel_id="C0123456")
        session_store.append_messages_batch.assert_not_awaited()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Cannot create channel for answer copy" in w and "telegram" in w for w in warnings)
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
