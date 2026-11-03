"""UserNotificationService: the message handed to the coordinator, the owner on history writes, and a failed history save.

`notify` builds one AgentMessage (empty `payload["text"]`, `context["thread_id"]` explicitly None); history appends
carry `owner_id`; a failing history save must not stop the rich content or turn a delivered notification into a failure.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentIntent, AgentResponse, AgentStatus
from src.domain.messaging import RichContent, SmartResponse
from src.domain.notification import NotificationChannel
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService

_USER_ID = "user-mut-1"
_ACCOUNT_ID = "account-mut-1"
_CHANNEL_ID = "C-mut-1"


@pytest.fixture
def channel() -> MagicMock:
    ch = MagicMock()
    ch.send_long_text = AsyncMock()
    ch.send_rich_content = AsyncMock()
    ch.send_document_link = AsyncMock()
    return ch


@pytest.fixture
def session_store() -> AsyncMock:
    store = AsyncMock()
    store.append_messages_batch = AsyncMock()
    return store


@pytest.fixture
def coordinator() -> AsyncMock:
    coord = AsyncMock()
    coord.route_message = AsyncMock()
    return coord


@pytest.fixture
def service(channel, session_store, coordinator) -> UserNotificationService:
    state = AsyncMock(spec=NotificationStatePort)
    state.get.return_value = NotificationChannel(
        user_id=_USER_ID, platform="telegram", channel_id=_CHANNEL_ID, updated_at=datetime(2026, 1, 1),
    )
    state.get_primary.return_value = None
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    return UserNotificationService(
        state_repo=state,
        channel_factory=factory,
        coordinator=coordinator,
        notification_sla=NOTIFICATION_SLA,
        session_store=session_store,
    )


def _respond(coordinator, result):
    coordinator.route_message.return_value = AgentResponse.success(task_id="t-mut-1", agent_id="a-mut-1", result=result)


class TestNotifyMessageShape:
    async def test_message_carries_empty_text_payload_and_an_explicit_null_thread(self, service, coordinator):
        _respond(coordinator, SmartResponse(text="Hello from the agent"))

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert-mut-1", kind=NotificationKind.REMINDER)

        message = coordinator.route_message.await_args.args[0]
        assert message.sender == "notification_service"
        assert message.intent == AgentIntent.QUERY
        assert message.payload == {"text": ""}
        assert "thread_id" in message.context
        assert message.context["thread_id"] is None
        assert message.context["user_id"] == _USER_ID


class TestOwnerOnHistoryWrites:
    async def test_notify_history_append_names_the_owner(self, service, coordinator, session_store):
        _respond(coordinator, SmartResponse(text="Hello from the agent"))

        await service.notify(
            _USER_ID, _ACCOUNT_ID, "alert-mut-1", kind=NotificationKind.REMINDER, session_id="session-mut-1",
        )

        kwargs = session_store.append_messages_batch.await_args.kwargs
        assert kwargs["session_id"] == "session-mut-1"
        assert kwargs["owner_id"] == _USER_ID

    async def test_document_link_history_append_names_the_owner(self, service, channel, session_store):
        await service.notify_document_link(
            _USER_ID, _ACCOUNT_ID, url="https://example.invalid/doc", label="Doc mut 1", key="docs/mut-1",
        )

        channel.send_document_link.assert_awaited_once()
        kwargs = session_store.append_messages_batch.await_args.kwargs
        assert kwargs["session_id"].startswith(_USER_ID)
        assert kwargs["owner_id"] == _USER_ID


class TestFailedHistorySave:
    async def test_rich_content_is_still_sent_and_the_notification_counts_as_delivered(
        self, service, channel, coordinator, session_store,
    ):
        rich = RichContent(content_type="table", data={"rows": [["a", "b"]]}, fallback_text="a b")
        _respond(coordinator, SmartResponse(text="Hello from the agent", structured_data=rich))
        session_store.append_messages_batch.side_effect = RuntimeError("store down")

        result = await service.notify(_USER_ID, _ACCOUNT_ID, "alert-mut-1", kind=NotificationKind.REMINDER)

        channel.send_long_text.assert_awaited_once()
        channel.send_rich_content.assert_awaited_once_with(rich)
        assert result.delivered is True
        assert result.sent is True
        assert result.agent_status == AgentStatus.SUCCESS
