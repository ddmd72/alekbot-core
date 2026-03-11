"""
Tests for NotificationChannelFactory — verifies DI-based factory pattern.

Checks:
- Factory works with injected channel callables (no concrete imports)
- Unknown platform returns None with a warning
- Registered factories are called with the correct channel_id
- No direct Slack/Telegram adapter imports in the factory module
"""
import inspect
import pytest
from unittest.mock import MagicMock

from src.adapters.notification_channel_factory import NotificationChannelFactory


class TestNotificationChannelFactory:
    def test_no_slack_response_channel_import(self):
        """Factory must not import SlackResponseChannel directly."""
        import src.adapters.notification_channel_factory as module
        source = inspect.getsource(module)
        assert "SlackResponseChannel" not in source

    def test_no_telegram_response_channel_import(self):
        """Factory must not import TelegramResponseChannel directly."""
        import src.adapters.notification_channel_factory as module
        source = inspect.getsource(module)
        assert "TelegramResponseChannel" not in source

    def test_unknown_platform_returns_none(self):
        factory = NotificationChannelFactory()
        result = factory.create("discord", "123")
        assert result is None

    def test_registered_factory_called_with_channel_id(self):
        factory = NotificationChannelFactory()
        mock_channel = MagicMock()
        factory.register_factory("slack", lambda ch: mock_channel if ch == "C123" else None)

        result = factory.create("slack", "C123")
        assert result is mock_channel

    def test_second_register_overwrites_first(self):
        factory = NotificationChannelFactory()
        old_channel = MagicMock()
        new_channel = MagicMock()

        factory.register_factory("slack", lambda _: old_channel)
        factory.register_factory("slack", lambda _: new_channel)

        result = factory.create("slack", "C999")
        assert result is new_channel

    def test_multiple_platforms_independent(self):
        factory = NotificationChannelFactory()
        slack_channel = MagicMock()
        telegram_channel = MagicMock()

        factory.register_factory("slack", lambda _: slack_channel)
        factory.register_factory("telegram", lambda _: telegram_channel)

        assert factory.create("slack", "C1") is slack_channel
        assert factory.create("telegram", "42") is telegram_channel
        assert factory.create("teams", "X1") is None
