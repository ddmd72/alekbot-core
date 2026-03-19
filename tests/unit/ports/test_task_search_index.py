"""
Port contract tests for TaskSearchIndex.

Covers:
- TaskSearchIndex (5 abstract async methods: upsert, delete, delete_by_list,
  find_nearest, delete_all_for_user)
- AsyncMock(spec=TaskSearchIndex) satisfies the port contract in service tests
"""

import inspect
from abc import ABC
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from src.domain.task import TaskImportance, TaskSearchEntry, TaskStatus
from src.ports.task_search_index import TaskSearchIndex


def _make_entry(task_id: str = "t1", user_id: str = "u1") -> TaskSearchEntry:
    return TaskSearchEntry(
        task_id=task_id,
        list_id="list-1",
        list_name="Alek Bot Tasks",
        user_id=user_id,
        title="Buy milk",
        status=TaskStatus.NOT_STARTED,
        tags=["shopping"],
        importance=TaskImportance.NORMAL,
        indexed_at=datetime(2026, 3, 18),
    )


class TestTaskSearchIndexContract:
    """Verify TaskSearchIndex declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(TaskSearchIndex, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TaskSearchIndex()

    def test_has_upsert(self):
        assert getattr(TaskSearchIndex.upsert, "__isabstractmethod__", False)

    def test_has_delete(self):
        assert getattr(TaskSearchIndex.delete, "__isabstractmethod__", False)

    def test_has_delete_by_list(self):
        assert getattr(TaskSearchIndex.delete_by_list, "__isabstractmethod__", False)

    def test_has_find_nearest(self):
        assert getattr(TaskSearchIndex.find_nearest, "__isabstractmethod__", False)

    def test_has_delete_all_for_user(self):
        assert getattr(TaskSearchIndex.delete_all_for_user, "__isabstractmethod__", False)

    def test_all_abstract_methods_are_async(self):
        for name in ("upsert", "delete", "delete_by_list", "find_nearest", "get_by_short_id", "delete_all_for_user"):
            method = getattr(TaskSearchIndex, name)
            assert inspect.iscoroutinefunction(method), f"{name} must be async"

    def test_abstract_method_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(TaskSearchIndex)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 6, f"Expected 6 abstract methods, got {abstract_methods}"

    def test_find_nearest_signature(self):
        sig = inspect.signature(TaskSearchIndex.find_nearest)
        params = list(sig.parameters.keys())
        assert "user_id" in params
        assert "vectors" in params
        assert "limit" in params
        assert "show_completed" in params
        assert "list_id" in params

    def test_upsert_signature(self):
        sig = inspect.signature(TaskSearchIndex.upsert)
        params = list(sig.parameters.keys())
        assert "entry" in params


class TestTaskSearchIndexMockImplementation:
    """Verify AsyncMock(spec=TaskSearchIndex) satisfies the port contract in service tests."""

    @pytest.fixture
    def mock_index(self):
        return AsyncMock(spec=TaskSearchIndex)

    async def test_upsert_called(self, mock_index):
        entry = _make_entry()
        mock_index.upsert.return_value = None
        await mock_index.upsert(entry)
        mock_index.upsert.assert_called_once_with(entry)

    async def test_delete_called(self, mock_index):
        mock_index.delete.return_value = None
        await mock_index.delete(user_id="u1", task_id="t1")
        mock_index.delete.assert_called_once_with(user_id="u1", task_id="t1")

    async def test_delete_by_list_called(self, mock_index):
        mock_index.delete_by_list.return_value = None
        await mock_index.delete_by_list(user_id="u1", list_id="list-1")
        mock_index.delete_by_list.assert_called_once_with(user_id="u1", list_id="list-1")

    async def test_find_nearest_returns_entries(self, mock_index):
        entries = [_make_entry("t1"), _make_entry("t2")]
        mock_index.find_nearest.return_value = entries
        result = await mock_index.find_nearest(
            user_id="u1",
            vectors={"content": [0.1, 0.2]},
            limit=10,
            show_completed=False,
        )
        assert len(result) == 2

    async def test_find_nearest_with_list_id_filter(self, mock_index):
        mock_index.find_nearest.return_value = [_make_entry()]
        result = await mock_index.find_nearest(
            user_id="u1",
            vectors={"content": [0.1]},
            list_id="list-1",
        )
        assert result[0].list_id == "list-1"

    async def test_delete_all_for_user_called(self, mock_index):
        mock_index.delete_all_for_user.return_value = None
        await mock_index.delete_all_for_user(user_id="u1")
        mock_index.delete_all_for_user.assert_called_once_with(user_id="u1")
