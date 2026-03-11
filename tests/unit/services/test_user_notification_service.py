"""
Unit tests for UserNotificationService.notify().

Focus: verifying that SmartResponse fields (link_list, structured_data) are
correctly delivered — regression for the bug where only .text was sent.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from datetime import datetime

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.messaging import RichContent, SmartResponse
from src.domain.notification import NotificationChannel
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc123"
_ACCOUNT_ID = "account-xyz"
_PLATFORM = "slack"
_CHANNEL_ID = "C0123456"

_LINK_LIST = [{"anchor": "1", "title": "Full Report", "url": "https://storage.example.com/report.html"}]
_RICH_CONTENT = RichContent(
    content_type="table",
    data={
        "title": "Results",
        "headers": ["Col A", "Col B"],
        "rows": [{"cells": ["val1", "val2"]}],
    },
    fallback_text="Results table",
)


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
    ch.send_rich_content = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.max_message_length = max_message_length
    return ch


def _make_success_response(result) -> AgentResponse:
    resp = MagicMock(spec=AgentResponse)
    resp.status = AgentStatus.SUCCESS
    resp.result = result
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_repo() -> AsyncMock:
    repo = AsyncMock(spec=NotificationStatePort)
    repo.get.return_value = _make_channel_info()
    return repo


@pytest.fixture
def response_channel() -> MagicMock:
    return _make_response_channel()


@pytest.fixture
def channel_factory(response_channel) -> MagicMock:
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = response_channel
    return factory


@pytest.fixture
def coordinator() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(state_repo, channel_factory, coordinator) -> UserNotificationService:
    return UserNotificationService(
        state_repo=state_repo,
        channel_factory=channel_factory,
        coordinator=coordinator,
    )


# ---------------------------------------------------------------------------
# Tests: notify() — SmartResponse delivery
# ---------------------------------------------------------------------------

class TestNotifySmartResponse:
    """notify() correctly delivers all three fields of SmartResponse."""

    async def test_text_only_smart_response(self, service, coordinator, response_channel):
        """Plain text SmartResponse: send_message called, no rich_content delivery."""
        smart = SmartResponse(text="Hello user")
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "some alert")

        response_channel.send_message.assert_awaited_once()
        call_args = response_channel.send_message.call_args
        assert call_args[0][0] == "Hello user"
        assert call_args[1].get("link_list") is None
        response_channel.send_rich_content.assert_not_awaited()

    async def test_link_list_delivered(self, service, coordinator, response_channel):
        """SmartResponse with link_list: send_message receives exact text and link_list."""
        smart = SmartResponse(text="See [Report][1].", link_list=_LINK_LIST)
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert with links")

        response_channel.send_message.assert_awaited_once()
        call_args = response_channel.send_message.call_args
        assert call_args[0][0] == "See [Report][1]."
        assert call_args[1]["link_list"] == _LINK_LIST
        response_channel.send_rich_content.assert_not_awaited()

    async def test_rich_content_delivered(self, service, coordinator, response_channel):
        """SmartResponse with structured_data: text delivered and send_rich_content called."""
        smart = SmartResponse(text="Here is your table.", structured_data=_RICH_CONTENT)
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert with table")

        response_channel.send_message.assert_awaited_once()
        assert response_channel.send_message.call_args[0][0] == "Here is your table."
        response_channel.send_rich_content.assert_awaited_once_with(_RICH_CONTENT)

    async def test_link_list_and_rich_content_delivered(self, service, coordinator, response_channel):
        """Deep research case: text + link_list + structured_data all delivered."""
        smart = SmartResponse(
            text="Research done. [Повний звіт][1].",
            link_list=_LINK_LIST,
            structured_data=_RICH_CONTENT,
        )
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "deep research complete")

        response_channel.send_message.assert_awaited_once()
        call_args = response_channel.send_message.call_args
        assert call_args[0][0] == "Research done. [Повний звіт][1]."
        assert call_args[1]["link_list"] == _LINK_LIST
        response_channel.send_rich_content.assert_awaited_once_with(_RICH_CONTENT)

    async def test_empty_link_list_not_forwarded(self, service, coordinator, response_channel):
        """Empty link_list → send_message gets link_list=None, not an empty list."""
        smart = SmartResponse(text="No links here.", link_list=[])
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "plain alert")

        call_kwargs = response_channel.send_message.call_args[1]
        assert call_kwargs.get("link_list") is None

    async def test_no_send_when_text_empty(self, service, coordinator, response_channel):
        """Empty text + rich_content: send_message not called, send_rich_content IS called."""
        smart = SmartResponse(text="", structured_data=_RICH_CONTENT)
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "table only alert")

        response_channel.send_message.assert_not_awaited()
        response_channel.send_rich_content.assert_awaited_once_with(_RICH_CONTENT)

    async def test_long_text_uses_chunked_delivery(self, service, coordinator, channel_factory, state_repo):
        """Text exceeding max_message_length: 📩 placeholder + send_chunked_message.

        notify() posts a bare emoji as the placeholder (no locale dependency),
        captures the ts, then expands via send_chunked_message.
        SlackResponseChannel.send_message normalizes user ID → real DM channel ID on the
        first post, so the subsequent chat.update inside send_chunked_message succeeds.
        """
        short_limit_channel = _make_response_channel(max_message_length=50)
        channel_factory.create.return_value = short_limit_channel

        long_text = "A" * 100  # exceeds limit of 50
        smart = SmartResponse(text=long_text, link_list=_LINK_LIST)
        coordinator.route_message.return_value = _make_success_response(smart)

        await service.notify(_USER_ID, _ACCOUNT_ID, "deep research alert")

        # First send_message call posts the placeholder emoji
        short_limit_channel.send_message.assert_awaited_once_with("📩")
        # send_chunked_message receives the full text + ts from placeholder
        short_limit_channel.send_chunked_message.assert_awaited_once()
        chunked_call = short_limit_channel.send_chunked_message.call_args
        assert chunked_call[0][0] == long_text
        assert chunked_call[0][1] == "msg-placeholder-ts"
        assert chunked_call[1].get("link_list") == _LINK_LIST


class TestNotifyLegacyStringResult:
    """notify() legacy path: result is a plain string, not SmartResponse."""

    async def test_string_result_delivered(self, service, coordinator, response_channel):
        """Plain string result: delivered as-is, no link_list, no rich_content."""
        coordinator.route_message.return_value = _make_success_response("Plain text answer")

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert")

        response_channel.send_message.assert_awaited_once()
        assert response_channel.send_message.call_args[0][0] == "Plain text answer"
        response_channel.send_rich_content.assert_not_awaited()

    async def test_none_result_not_delivered(self, service, coordinator, response_channel):
        """None result: nothing sent to channel."""
        coordinator.route_message.return_value = _make_success_response(None)

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert")

        response_channel.send_message.assert_not_awaited()
        response_channel.send_rich_content.assert_not_awaited()


class TestNotifyEarlyExits:
    """notify() silently skips when channel is unavailable."""

    async def test_no_channel_stored(self, service, state_repo, coordinator):
        """No stored channel → coordinator never called."""
        state_repo.get.return_value = None

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert")

        coordinator.route_message.assert_not_awaited()

    async def test_factory_returns_none(self, service, channel_factory, coordinator):
        """Factory returns None (unknown platform) → coordinator never called."""
        channel_factory.create.return_value = None

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert")

        coordinator.route_message.assert_not_awaited()

    async def test_agent_failure_not_delivered(self, service, coordinator, response_channel):
        """Agent returns FAILURE → nothing sent to channel."""
        fail_resp = MagicMock(spec=AgentResponse)
        fail_resp.status = AgentStatus.FAILED
        coordinator.route_message.return_value = fail_resp

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert")

        response_channel.send_message.assert_not_awaited()
        response_channel.send_rich_content.assert_not_awaited()

    async def test_state_repo_error_swallowed(self, service, state_repo, coordinator):
        """state_repo.get raises → silently returns, no crash."""
        state_repo.get.side_effect = RuntimeError("db offline")

        await service.notify(_USER_ID, _ACCOUNT_ID, "alert")  # must not raise

        coordinator.route_message.assert_not_awaited()


class TestNotifyRaw:
    """notify_raw() delivers text directly without agent routing."""

    async def test_raw_text_delivered(self, service, state_repo, channel_factory, response_channel):
        """notify_raw: send_message called with exact text."""
        await service.notify_raw(_USER_ID, _ACCOUNT_ID, "📄 Full report: https://example.com")

        response_channel.send_message.assert_awaited_once_with(
            "📄 Full report: https://example.com"
        )
        response_channel.send_rich_content.assert_not_awaited()

    async def test_raw_no_channel_skips(self, service, state_repo, response_channel):
        """notify_raw with no stored channel → nothing sent."""
        state_repo.get.return_value = None

        await service.notify_raw(_USER_ID, _ACCOUNT_ID, "report link")

        response_channel.send_message.assert_not_awaited()
