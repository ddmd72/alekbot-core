"""
Port contract tests for TasksProviderPort (MS To Do integration).

Covers:
- TasksProviderPort (7 abstract async methods: list_task_lists, list_tasks,
  get_task, batch_get_tasks, create_task, update_task, delete_task)
- search_tasks removed: semantic search is handled by TaskSearchIndex
- AsyncMock(spec=TasksProviderPort) satisfies the port contract in agent tests
"""

import inspect
from abc import ABC
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.domain.task import (
    Task,
    TaskCreate,
    TaskImportance,
    TaskList,
    TaskStatus,
    TaskUpdate,
)
from src.ports.tasks_provider_port import TasksProviderPort


def _make_task(
    task_id: str = "t1",
    list_id: str = "list-1",
    title: str = "Buy milk",
    status: TaskStatus = TaskStatus.NOT_STARTED,
) -> Task:
    return Task(
        task_id=task_id,
        list_id=list_id,
        list_name="Alek Bot Tasks",
        user_id="u1",
        title=title,
        status=status,
    )


class TestTasksProviderPortContract:
    """Verify TasksProviderPort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(TasksProviderPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TasksProviderPort()

    def test_has_list_task_lists(self):
        assert getattr(TasksProviderPort.list_task_lists, "__isabstractmethod__", False)

    def test_has_list_tasks(self):
        assert getattr(TasksProviderPort.list_tasks, "__isabstractmethod__", False)

    def test_has_get_task(self):
        assert getattr(TasksProviderPort.get_task, "__isabstractmethod__", False)

    def test_has_batch_get_tasks(self):
        assert getattr(TasksProviderPort.batch_get_tasks, "__isabstractmethod__", False)

    def test_has_create_task(self):
        assert getattr(TasksProviderPort.create_task, "__isabstractmethod__", False)

    def test_has_update_task(self):
        assert getattr(TasksProviderPort.update_task, "__isabstractmethod__", False)

    def test_has_delete_task(self):
        assert getattr(TasksProviderPort.delete_task, "__isabstractmethod__", False)

    def test_has_no_search_tasks(self):
        """search_tasks removed — semantic search is handled by TaskSearchIndex."""
        assert not hasattr(TasksProviderPort, "search_tasks")

    def test_all_abstract_methods_are_async(self):
        for name in (
            "list_task_lists", "list_tasks", "get_task", "batch_get_tasks",
            "create_task", "update_task", "delete_task",
        ):
            method = getattr(TasksProviderPort, name)
            assert inspect.iscoroutinefunction(method), f"{name} must be async"

    def test_abstract_method_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(TasksProviderPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 7, f"Expected 7 abstract methods, got {abstract_methods}"

    def test_list_tasks_signature(self):
        sig = inspect.signature(TasksProviderPort.list_tasks)
        params = sig.parameters
        assert "user_id" in params
        assert "list_id" in params
        assert "show_completed" in params

    def test_update_task_signature(self):
        sig = inspect.signature(TasksProviderPort.update_task)
        params = sig.parameters
        assert "user_id" in params
        assert "list_id" in params
        assert "task_id" in params
        assert "updates" in params

    def test_delete_task_signature(self):
        sig = inspect.signature(TasksProviderPort.delete_task)
        params = sig.parameters
        assert "user_id" in params
        assert "list_id" in params
        assert "task_id" in params

    def test_batch_get_tasks_signature(self):
        sig = inspect.signature(TasksProviderPort.batch_get_tasks)
        params = sig.parameters
        assert "user_id" in params
        assert "task_refs" in params


class TestTasksProviderPortMockImplementation:
    """Verify AsyncMock(spec=TasksProviderPort) satisfies the port contract in agent tests."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=TasksProviderPort)

    async def test_list_task_lists_returns_list(self, mock_port):
        mock_port.list_task_lists.return_value = [
            TaskList(list_id="list-1", name="Alek Bot Tasks")
        ]
        result = await mock_port.list_task_lists(user_id="u1")
        assert len(result) == 1
        assert result[0].list_id == "list-1"

    async def test_list_tasks_returns_list(self, mock_port):
        mock_port.list_tasks.return_value = [_make_task()]
        result = await mock_port.list_tasks(user_id="u1", show_completed=False)
        assert result[0].title == "Buy milk"

    async def test_list_tasks_with_list_id(self, mock_port):
        mock_port.list_tasks.return_value = [_make_task()]
        await mock_port.list_tasks(user_id="u1", list_id="list-1", show_completed=False)
        mock_port.list_tasks.assert_called_once_with(
            user_id="u1", list_id="list-1", show_completed=False
        )

    async def test_get_task_returns_task(self, mock_port):
        mock_port.get_task.return_value = _make_task(task_id="t1")
        result = await mock_port.get_task(user_id="u1", list_id="list-1", task_id="t1")
        assert result.task_id == "t1"

    async def test_get_task_raises_on_not_found(self, mock_port):
        mock_port.get_task.side_effect = ValueError("Task not found")
        with pytest.raises(ValueError, match="not found"):
            await mock_port.get_task(user_id="u1", list_id="list-1", task_id="missing")

    async def test_batch_get_tasks_returns_multiple(self, mock_port):
        tasks = [_make_task("t1"), _make_task("t2")]
        mock_port.batch_get_tasks.return_value = tasks
        result = await mock_port.batch_get_tasks(
            user_id="u1", task_refs=[("list-1", "t1"), ("list-1", "t2")]
        )
        assert len(result) == 2

    async def test_create_task_returns_task(self, mock_port):
        created = _make_task(task_id="new-id", title="Pay bills")
        mock_port.create_task.return_value = created
        result = await mock_port.create_task(
            user_id="u1", task=TaskCreate(title="Pay bills")
        )
        assert result.task_id == "new-id"

    async def test_update_task_returns_updated(self, mock_port):
        updated = _make_task(task_id="t1", status=TaskStatus.COMPLETED)
        mock_port.update_task.return_value = updated
        result = await mock_port.update_task(
            user_id="u1", list_id="list-1", task_id="t1",
            updates=TaskUpdate(status=TaskStatus.COMPLETED),
        )
        assert result.status == TaskStatus.COMPLETED

    async def test_delete_task_returns_none(self, mock_port):
        mock_port.delete_task.return_value = None
        result = await mock_port.delete_task(
            user_id="u1", list_id="list-1", task_id="t1"
        )
        assert result is None
