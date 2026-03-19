"""
Port contract tests for TaskConfigPort.

Covers:
- TaskConfigPort (3 abstract async methods: get_config, save_config,
  set_primary_list_id_if_absent)
- AsyncMock(spec=TaskConfigPort) satisfies the port contract in service tests
"""

import inspect
from abc import ABC
from unittest.mock import AsyncMock

import pytest

from src.domain.task import TaskUserConfig
from src.ports.task_config_port import TaskConfigPort


class TestTaskConfigPortContract:
    """Verify TaskConfigPort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(TaskConfigPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TaskConfigPort()

    def test_has_get_config(self):
        assert getattr(TaskConfigPort.get_config, "__isabstractmethod__", False)

    def test_has_save_config(self):
        assert getattr(TaskConfigPort.save_config, "__isabstractmethod__", False)

    def test_has_set_primary_list_id_if_absent(self):
        assert getattr(TaskConfigPort.set_primary_list_id_if_absent, "__isabstractmethod__", False)

    def test_all_abstract_methods_are_async(self):
        for name in ("get_config", "save_config", "set_primary_list_id_if_absent"):
            method = getattr(TaskConfigPort, name)
            assert inspect.iscoroutinefunction(method), f"{name} must be async"

    def test_abstract_method_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(TaskConfigPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 3, f"Expected 3 abstract methods, got {abstract_methods}"

    def test_get_config_signature(self):
        sig = inspect.signature(TaskConfigPort.get_config)
        assert "user_id" in sig.parameters

    def test_save_config_signature(self):
        sig = inspect.signature(TaskConfigPort.save_config)
        params = sig.parameters
        assert "user_id" in params
        assert "config" in params

    def test_set_primary_list_id_if_absent_signature(self):
        sig = inspect.signature(TaskConfigPort.set_primary_list_id_if_absent)
        params = sig.parameters
        assert "user_id" in params
        assert "list_id" in params


class TestTaskConfigPortMockImplementation:
    """Verify AsyncMock(spec=TaskConfigPort) satisfies the port contract in service tests."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=TaskConfigPort)

    async def test_get_config_returns_empty_when_not_found(self, mock_port):
        mock_port.get_config.return_value = TaskUserConfig()
        config = await mock_port.get_config(user_id="u1")
        assert config.primary_list_id is None
        assert config.subscriptions == []

    async def test_get_config_returns_existing(self, mock_port):
        mock_port.get_config.return_value = TaskUserConfig(primary_list_id="list-1")
        config = await mock_port.get_config(user_id="u1")
        assert config.primary_list_id == "list-1"

    async def test_save_config_called(self, mock_port):
        config = TaskUserConfig(primary_list_id="list-1")
        mock_port.save_config.return_value = None
        await mock_port.save_config(user_id="u1", config=config)
        mock_port.save_config.assert_called_once_with(user_id="u1", config=config)

    async def test_set_primary_list_id_if_absent_returns_list_id(self, mock_port):
        mock_port.set_primary_list_id_if_absent.return_value = "list-new"
        result = await mock_port.set_primary_list_id_if_absent(user_id="u1", list_id="list-new")
        assert result == "list-new"

    async def test_set_primary_list_id_if_absent_returns_existing(self, mock_port):
        """When list_id already set, port returns existing value unchanged."""
        mock_port.set_primary_list_id_if_absent.return_value = "list-existing"
        result = await mock_port.set_primary_list_id_if_absent(user_id="u1", list_id="list-new")
        assert result == "list-existing"
