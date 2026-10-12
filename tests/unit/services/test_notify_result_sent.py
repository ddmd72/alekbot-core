"""`NotifyResult.sent` says whether `notify()` reached a channel send, so a caller that flags "user told" before
the send can roll the flag back when nothing went out (USER_DRIVE_INDEXING_RFC §7.7; the Gmail reconnect notice
follows the same rule). `sent` is set just before the first send: a send that raises still counts, because the
message may have left."""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.messaging import RichContent, SmartResponse
from src.domain.notification import NotificationChannel
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService

_RICH = RichContent(content_type="table", data={"title": "t", "headers": ["a"], "rows": [{"cells": ["1"]}]},
                    fallback_text="t")


@pytest.fixture
def channel():
    ch = MagicMock()
    ch.send_long_text = AsyncMock()
    ch.send_rich_content = AsyncMock()
    return ch


@pytest.fixture
def env(channel):
    state = AsyncMock(spec=NotificationStatePort)
    state.get.return_value = NotificationChannel(user_id="u", platform="telegram", channel_id="42",
                                                 updated_at=datetime(2026, 1, 1))
    state.get_primary.return_value = None
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    coordinator = AsyncMock()
    svc = UserNotificationService(state_repo=state, channel_factory=factory, coordinator=coordinator,
                                  notification_sla=NOTIFICATION_SLA)
    return svc, state, factory, coordinator


def _answer(coordinator, result, status=AgentStatus.SUCCESS):
    resp = MagicMock(spec=AgentResponse)
    resp.status = status
    resp.result = result
    coordinator.route_message.return_value = resp


async def _notify(svc):
    return await svc.notify("u", "a", "alert", kind=NotificationKind.DOCUMENT_DELIVERY)


async def test_delivered_text_is_sent(env):
    svc, _, _, coordinator = env
    _answer(coordinator, SmartResponse(text="hi"))
    result = await _notify(svc)
    assert result.delivered and result.sent


async def test_formatter_failure_sends_nothing(env, channel):
    svc, _, _, coordinator = env
    _answer(coordinator, None, status=AgentStatus.TIMEOUT)
    result = await _notify(svc)
    assert result.delivered is False and result.sent is False
    channel.send_long_text.assert_not_awaited()


async def test_routing_that_raises_sends_nothing(env):
    svc, _, _, coordinator = env
    coordinator.route_message.side_effect = RuntimeError("router down")
    result = await _notify(svc)
    assert result.delivered is False and result.sent is False


@pytest.mark.parametrize("stop", ["no_channel", "no_factory"])
async def test_no_channel_sends_nothing(env, stop):
    svc, state, factory, _ = env
    if stop == "no_channel":
        state.get.return_value = None
    else:
        factory.create.return_value = None
    result = await _notify(svc)
    assert result.delivered is False and result.sent is False


async def test_a_text_send_that_raises_counts_as_sent(env, channel):
    """The message may have left before the error: never assume it did not."""
    svc, _, _, coordinator = env
    _answer(coordinator, SmartResponse(text="hi"))
    channel.send_long_text.side_effect = RuntimeError("timeout after write")
    result = await _notify(svc)
    assert result.delivered is False and result.sent is True


async def test_rich_content_failing_after_the_text_counts_as_sent(env, channel):
    svc, _, _, coordinator = env
    _answer(coordinator, SmartResponse(text="hi", structured_data=_RICH))
    channel.send_rich_content.side_effect = RuntimeError("invalid_blocks")
    result = await _notify(svc)
    assert result.delivered is False and result.sent is True


async def test_rich_content_alone_that_raises_counts_as_sent(env, channel):
    svc, _, _, coordinator = env
    _answer(coordinator, SmartResponse(text="", structured_data=_RICH))
    channel.send_rich_content.side_effect = RuntimeError("invalid_blocks")
    result = await _notify(svc)
    assert result.sent is True
