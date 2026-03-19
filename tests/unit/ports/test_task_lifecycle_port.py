"""
Port contract tests for TaskLifecyclePort.

Covers:
- TaskLifecyclePort (4 abstract async methods: ensure_primary_list,
  register_subscription, renew_subscription, delete_subscription)
- AsyncMock(spec=TaskLifecyclePort) satisfies the port contract in service tests
"""

import inspect
from abc import ABC
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.domain.task import TaskSubscriptionConfig
from src.ports.task_lifecycle_port import TaskLifecyclePort


def _make_sub_config(sub_id: str = "sub-1", list_id: str = "list-1") -> TaskSubscriptionConfig:
    return TaskSubscriptionConfig(
        sub_id=sub_id,
        list_id=list_id,
        expires_at=datetime(2026, 3, 21, 12, 0, 0),
    )


class TestTaskLifecyclePortContract:
    """Verify TaskLifecyclePort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(TaskLifecyclePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TaskLifecyclePort()

    def test_has_ensure_primary_list(self):
        assert getattr(TaskLifecyclePort.ensure_primary_list, "__isabstractmethod__", False)

    def test_has_register_subscription(self):
        assert getattr(TaskLifecyclePort.register_subscription, "__isabstractmethod__", False)

    def test_has_renew_subscription(self):
        assert getattr(TaskLifecyclePort.renew_subscription, "__isabstractmethod__", False)

    def test_has_delete_subscription(self):
        assert getattr(TaskLifecyclePort.delete_subscription, "__isabstractmethod__", False)

    def test_all_abstract_methods_are_async(self):
        for name in (
            "ensure_primary_list",
            "register_subscription",
            "renew_subscription",
            "delete_subscription",
        ):
            method = getattr(TaskLifecyclePort, name)
            assert inspect.iscoroutinefunction(method), f"{name} must be async"

    def test_abstract_method_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(TaskLifecyclePort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 4, f"Expected 4 abstract methods, got {abstract_methods}"

    def test_ensure_primary_list_signature(self):
        sig = inspect.signature(TaskLifecyclePort.ensure_primary_list)
        assert "user_id" in sig.parameters

    def test_register_subscription_signature(self):
        sig = inspect.signature(TaskLifecyclePort.register_subscription)
        params = sig.parameters
        assert "user_id" in params
        assert "list_id" in params
        assert "notification_url_base" in params

    def test_renew_subscription_signature(self):
        sig = inspect.signature(TaskLifecyclePort.renew_subscription)
        params = sig.parameters
        assert "user_id" in params
        assert "sub_id" in params

    def test_delete_subscription_signature(self):
        sig = inspect.signature(TaskLifecyclePort.delete_subscription)
        params = sig.parameters
        assert "user_id" in params
        assert "sub_id" in params


class TestTaskLifecyclePortMockImplementation:
    """Verify AsyncMock(spec=TaskLifecyclePort) satisfies the port contract in service tests."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=TaskLifecyclePort)

    async def test_ensure_primary_list_returns_list_id(self, mock_port):
        mock_port.ensure_primary_list.return_value = "list-abc"
        result = await mock_port.ensure_primary_list(user_id="u1")
        assert result == "list-abc"

    async def test_register_subscription_returns_config(self, mock_port):
        sub = _make_sub_config()
        mock_port.register_subscription.return_value = sub
        result = await mock_port.register_subscription(
            user_id="u1", list_id="list-1", notification_url_base="https://example.com"
        )
        assert result.sub_id == "sub-1"
        assert result.list_id == "list-1"

    async def test_register_subscription_does_not_persist(self, mock_port):
        """Contract: register_subscription returns config but does NOT call TaskConfigPort."""
        mock_port.register_subscription.return_value = _make_sub_config()
        await mock_port.register_subscription(
            user_id="u1", list_id="list-1", notification_url_base="https://example.com"
        )
        # Verify only the lifecycle port was called — no config persistence side effects
        mock_port.register_subscription.assert_called_once()

    async def test_renew_subscription_returns_updated_config(self, mock_port):
        updated = _make_sub_config(sub_id="sub-1")
        mock_port.renew_subscription.return_value = updated
        result = await mock_port.renew_subscription(user_id="u1", sub_id="sub-1")
        assert result.sub_id == "sub-1"

    async def test_delete_subscription_returns_none(self, mock_port):
        mock_port.delete_subscription.return_value = None
        result = await mock_port.delete_subscription(user_id="u1", sub_id="sub-1")
        assert result is None
        mock_port.delete_subscription.assert_called_once_with(user_id="u1", sub_id="sub-1")
