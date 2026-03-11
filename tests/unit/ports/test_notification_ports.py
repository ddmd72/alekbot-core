"""
Port contract tests for notification-related ports.

Covers:
- NotificationChannelFactoryPort (1 abstract method: create — sync)
- NotificationStatePort (2 abstract methods: save, get — both async)
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock, MagicMock

from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.domain.notification import NotificationChannel


# =============================================================================
# NotificationChannelFactoryPort
# =============================================================================

class TestNotificationChannelFactoryPortContract:
    """Verify NotificationChannelFactoryPort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(NotificationChannelFactoryPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            NotificationChannelFactoryPort()

    def test_has_create(self):
        assert getattr(NotificationChannelFactoryPort.create, "__isabstractmethod__", False)

    def test_create_is_sync(self):
        assert not inspect.iscoroutinefunction(NotificationChannelFactoryPort.create)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(NotificationChannelFactoryPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 1, f"Expected 1 abstract method, got {abstract_methods}"

    def test_create_signature(self):
        sig = inspect.signature(NotificationChannelFactoryPort.create)
        params = list(sig.parameters.keys())
        assert params == ["self", "platform", "channel_id"]


class TestNotificationChannelFactoryPortMockImplementation:
    """Verify MagicMock(spec=NotificationChannelFactoryPort) satisfies the port contract."""

    @pytest.fixture
    def mock_factory(self):
        return MagicMock(spec=NotificationChannelFactoryPort)

    def test_create_returns_none_for_unconfigured_platform(self, mock_factory):
        mock_factory.create.return_value = None
        result = mock_factory.create(platform="unknown", channel_id="C123")
        assert result is None

    def test_create_returns_channel_for_known_platform(self, mock_factory):
        mock_channel = MagicMock()
        mock_factory.create.return_value = mock_channel
        result = mock_factory.create(platform="slack", channel_id="C123")
        assert result is mock_channel
        mock_factory.create.assert_called_once_with(platform="slack", channel_id="C123")


# =============================================================================
# NotificationStatePort
# =============================================================================

class TestNotificationStatePortContract:
    """Verify NotificationStatePort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(NotificationStatePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            NotificationStatePort()

    def test_has_save(self):
        assert getattr(NotificationStatePort.save, "__isabstractmethod__", False)

    def test_has_get(self):
        assert getattr(NotificationStatePort.get, "__isabstractmethod__", False)

    def test_both_methods_are_async(self):
        assert inspect.iscoroutinefunction(NotificationStatePort.save)
        assert inspect.iscoroutinefunction(NotificationStatePort.get)

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(NotificationStatePort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_save_signature(self):
        sig = inspect.signature(NotificationStatePort.save)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id", "platform", "channel_id"]

    def test_get_signature(self):
        sig = inspect.signature(NotificationStatePort.get)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id"]


class TestNotificationStatePortMockImplementation:
    """Verify AsyncMock(spec=NotificationStatePort) satisfies the port contract."""

    @pytest.fixture
    def mock_state(self):
        return AsyncMock(spec=NotificationStatePort)

    async def test_save_called(self, mock_state):
        await mock_state.save(user_id="u1", platform="slack", channel_id="C123")
        mock_state.save.assert_called_once_with(
            user_id="u1", platform="slack", channel_id="C123"
        )

    async def test_get_returns_none_when_no_state(self, mock_state):
        mock_state.get.return_value = None
        result = await mock_state.get(user_id="u1")
        assert result is None

    async def test_get_returns_notification_channel(self, mock_state):
        channel = MagicMock(spec=NotificationChannel)
        mock_state.get.return_value = channel
        result = await mock_state.get(user_id="u1")
        assert result is channel
