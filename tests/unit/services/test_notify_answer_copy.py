"""
Unit tests for UserNotificationService.notify_answer_copy() — Task 12: chat copy
of a reading-shaped answer Alek gives during a phone call (VOICE_COMPANION_RFC
§4.10 rule 2: links/tables always reach chat). Delivery only, never writes
history, never raises.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from datetime import datetime

from src.domain.messaging import SmartResponse
from src.domain.notification import NotificationChannel
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService

_USER_ID = "user-abc123"
_ACCOUNT_ID = "account-xyz"
_PLATFORM = "slack"
_CHANNEL_ID = "C0123456"


def _make_channel_info() -> NotificationChannel:
    return NotificationChannel(
        user_id=_USER_ID,
        platform=_PLATFORM,
        channel_id=_CHANNEL_ID,
        updated_at=datetime(2026, 1, 1),
    )


def _make_response_channel(max_message_length: int = 4000) -> MagicMock:
    ch = MagicMock()
    ch.send_message = AsyncMock(return_value={"ts": "msg-placeholder-ts", "channel": "D0123456"})
    ch.send_long_text = AsyncMock(return_value={"ts": "msg-placeholder-ts", "channel": "D0123456"})
    ch.send_rich_content = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.max_message_length = max_message_length
    return ch


@pytest.fixture
def state_repo() -> AsyncMock:
    repo = AsyncMock(spec=NotificationStatePort)
    repo.get.return_value = _make_channel_info()
    repo.get_primary.return_value = None  # No explicit primary — fall through to last-active
    return repo


@pytest.fixture
def channel() -> MagicMock:
    return _make_response_channel()


@pytest.fixture
def channel_factory(channel) -> MagicMock:
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
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
        notification_sla=NOTIFICATION_SLA,
        session_store=session_store,
    )


class TestNotifyAnswerCopy:

    async def test_answer_copy_sends_text_links_and_table_without_writing_history(
        self, service, channel, session_store
    ):
        table = MagicMock()
        answer = SmartResponse(
            text="Your flights [1]",
            structured_data=table,
            link_list=[{"anchor": 1, "title": "Iberia", "url": "https://x"}],
        )
        await service.notify_answer_copy("u1", "a1", answer)

        channel.send_long_text.assert_awaited_once()
        text = channel.send_long_text.await_args.args[0]
        assert text.startswith("📞 ") and "Your flights [1]" in text
        assert channel.send_long_text.await_args.kwargs["link_list"] == answer.link_list
        channel.send_rich_content.assert_awaited_once_with(table)
        session_store.append_messages_batch.assert_not_awaited()

    async def test_answer_copy_never_raises(self, service, channel):
        channel.send_long_text.side_effect = RuntimeError("slack down")
        await service.notify_answer_copy(
            "u1", "a1", SmartResponse(text="t", structured_data=None, link_list=[{"anchor": 1}]),
        )  # must not raise

    async def test_answer_copy_no_channel_no_send_no_raise(self, service, state_repo, channel):
        state_repo.get.return_value = None
        state_repo.get_primary.return_value = None

        await service.notify_answer_copy(
            "u1", "a1", SmartResponse(text="t", structured_data=None, link_list=[]),
        )  # must not raise

        channel.send_long_text.assert_not_awaited()
        channel.send_rich_content.assert_not_awaited()

    async def test_answer_copy_no_links_sends_none(self, service, channel):
        answer = SmartResponse(text="plain answer", structured_data=None, link_list=[])
        await service.notify_answer_copy("u1", "a1", answer)

        channel.send_long_text.assert_awaited_once()
        assert channel.send_long_text.await_args.kwargs["link_list"] is None
        channel.send_rich_content.assert_not_awaited()

    async def test_answer_copy_slack_dm_prepends_mention(self, service, state_repo, channel):
        state_repo.get.return_value = NotificationChannel(
            user_id=_USER_ID,
            platform="slack",
            channel_id="U1234567",  # user ID, not DM channel
            updated_at=datetime(2026, 1, 1),
        )
        answer = SmartResponse(text="Your flights", structured_data=None, link_list=[])
        await service.notify_answer_copy("u1", "a1", answer)

        text = channel.send_long_text.await_args.args[0]
        assert text.startswith("<@U1234567> 📞 Your flights")
