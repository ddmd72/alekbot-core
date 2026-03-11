"""
Port contract tests for TasksProviderPort.

Covers:
- TasksProviderPort (5 abstract async methods: list_tasks, create_task, update_task,
  delete_task, search_tasks)
- AsyncMock(spec=TasksProviderPort) satisfies the port contract in agent tests
"""

import inspect
import pytest
from abc import ABC
from datetime import datetime
from unittest.mock import AsyncMock

from src.domain.task import Task, TaskCreate, TaskStatus, TaskUpdate
from src.ports.tasks_provider_port import TasksProviderPort


class TestTasksProviderPortContract:
    """Verify TasksProviderPort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(TasksProviderPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TasksProviderPort()

    def test_has_list_tasks(self):
        assert getattr(TasksProviderPort.list_tasks, "__isabstractmethod__", False)

    def test_has_create_task(self):
        assert getattr(TasksProviderPort.create_task, "__isabstractmethod__", False)

    def test_has_update_task(self):
        assert getattr(TasksProviderPort.update_task, "__isabstractmethod__", False)

    def test_has_delete_task(self):
        assert getattr(TasksProviderPort.delete_task, "__isabstractmethod__", False)

    def test_has_search_tasks(self):
        assert getattr(TasksProviderPort.search_tasks, "__isabstractmethod__", False)

    def test_all_abstract_methods_are_async(self):
        for name in ("list_tasks", "create_task", "update_task", "delete_task", "search_tasks"):
            method = getattr(TasksProviderPort, name)
            assert inspect.iscoroutinefunction(method), f"{name} must be async"

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(TasksProviderPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_list_tasks_signature(self):
        sig = inspect.signature(TasksProviderPort.list_tasks)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "user_id" in params
        assert "show_completed" in params

    def test_create_task_signature(self):
        sig = inspect.signature(TasksProviderPort.create_task)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "user_id" in params
        assert "task" in params

    def test_update_task_signature(self):
        sig = inspect.signature(TasksProviderPort.update_task)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "user_id" in params
        assert "task_id" in params
        assert "updates" in params

    def test_delete_task_signature(self):
        sig = inspect.signature(TasksProviderPort.delete_task)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "user_id" in params
        assert "task_id" in params

    def test_search_tasks_signature(self):
        sig = inspect.signature(TasksProviderPort.search_tasks)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "user_id" in params
        assert "query" in params


class TestTasksProviderPortMockImplementation:
    """Verify AsyncMock(spec=TasksProviderPort) satisfies the port contract in agent tests."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=TasksProviderPort)

    def _make_task(self, task_id: str = "task-1", title: str = "Buy milk") -> Task:
        return Task(
            task_id=task_id,
            title=title,
            status=TaskStatus.NEEDS_ACTION,
            provider="google_tasks",
        )

    async def test_list_tasks_returns_list(self, mock_port):
        mock_port.list_tasks.return_value = [self._make_task()]
        result = await mock_port.list_tasks(user_id="u1", show_completed=False)
        assert isinstance(result, list)
        assert result[0].title == "Buy milk"

    async def test_create_task_returns_task(self, mock_port):
        task = self._make_task(task_id="new-id", title="New task")
        mock_port.create_task.return_value = task
        create = TaskCreate(title="New task")
        result = await mock_port.create_task(user_id="u1", task=create)
        assert result.task_id == "new-id"

    async def test_update_task_returns_task(self, mock_port):
        task = self._make_task(task_id="task-1", title="Updated")
        mock_port.update_task.return_value = task
        updates = TaskUpdate(title="Updated")
        result = await mock_port.update_task(user_id="u1", task_id="task-1", updates=updates)
        assert result.title == "Updated"

    async def test_delete_task_returns_none(self, mock_port):
        mock_port.delete_task.return_value = None
        result = await mock_port.delete_task(user_id="u1", task_id="task-1")
        assert result is None

    async def test_search_tasks_returns_list(self, mock_port):
        mock_port.search_tasks.return_value = [self._make_task(title="Groceries")]
        result = await mock_port.search_tasks(user_id="u1", query="groceries")
        assert len(result) == 1
        assert result[0].title == "Groceries"

    async def test_raises_value_error_on_not_found(self, mock_port):
        mock_port.delete_task.side_effect = ValueError("Task not-found not found")
        with pytest.raises(ValueError, match="not found"):
            await mock_port.delete_task(user_id="u1", task_id="not-found")
