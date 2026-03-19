"""
Unit tests for TaskIndexingService.

Mock boundary: ports (EmbeddingService, TaskSearchIndex, TasksProviderPort).

Covers:
- index_task: embeds title+body+checklist and list_name+tags+importance
- index_task: calls search_index.upsert with correct TaskSearchEntry
- deindex_task: calls search_index.delete
- index_task_by_ref: fetches task then indexes
- reindex_list: indexes all tasks with bounded concurrency
- search: embeds query and calls find_nearest with both vectors
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.task import (
    ChecklistItem,
    Task,
    TaskImportance,
    TaskSearchEntry,
    TaskStatus,
)
from src.ports.embedding_service import EmbeddingService
from src.ports.task_search_index import TaskSearchIndex
from src.ports.tasks_provider_port import TasksProviderPort
from src.services.task_indexing_service import TaskIndexingService

_USER_ID = "user-1"
_LIST_ID = "list-1"
_TASK_ID = "task-1"
_VECTOR = [0.1, 0.2, 0.3]


def _make_task(**kwargs) -> Task:
    defaults = dict(
        task_id=_TASK_ID,
        list_id=_LIST_ID,
        list_name="Alek Bot Tasks",
        user_id=_USER_ID,
        title="Buy milk",
        status=TaskStatus.NOT_STARTED,
        tags=["shopping"],
        importance=TaskImportance.NORMAL,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def _make_service():
    embedding = AsyncMock(spec=EmbeddingService)
    embedding.get_embedding.return_value = _VECTOR

    index = AsyncMock(spec=TaskSearchIndex)
    index.upsert.return_value = None
    index.delete.return_value = None
    index.find_nearest.return_value = []

    provider = AsyncMock(spec=TasksProviderPort)

    svc = TaskIndexingService(
        embedding_service=embedding,
        search_index=index,
        tasks_provider=provider,
    )
    return svc, embedding, index, provider


# =============================================================================
# index_task
# =============================================================================


class TestIndexTask:

    async def test_calls_embedding_twice(self):
        svc, embedding, _, _ = _make_service()
        task = _make_task()

        await svc.index_task(task)

        assert embedding.get_embedding.call_count == 2

    async def test_upsert_called_once(self):
        svc, _, index, _ = _make_service()
        task = _make_task()

        await svc.index_task(task)

        index.upsert.assert_called_once()

    async def test_upsert_entry_has_correct_task_id(self):
        svc, _, index, _ = _make_service()
        task = _make_task()

        await svc.index_task(task)

        entry: TaskSearchEntry = index.upsert.call_args.args[0]
        assert entry.task_id == _TASK_ID

    async def test_upsert_entry_has_vectors(self):
        svc, _, index, _ = _make_service()
        task = _make_task()

        await svc.index_task(task)

        entry: TaskSearchEntry = index.upsert.call_args.args[0]
        assert entry.content_vector == _VECTOR
        assert entry.context_vector == _VECTOR

    async def test_content_text_includes_title(self):
        svc, embedding, _, _ = _make_service()
        task = _make_task(title="Pay rent")

        await svc.index_task(task)

        calls = [str(c) for c in embedding.get_embedding.call_args_list]
        assert any("Pay rent" in c for c in calls)

    async def test_content_text_includes_body(self):
        svc, embedding, _, _ = _make_service()
        task = _make_task(body="Do it before the 5th")

        await svc.index_task(task)

        calls = [str(c) for c in embedding.get_embedding.call_args_list]
        assert any("Do it before the 5th" in c for c in calls)

    async def test_content_text_includes_checklist(self):
        svc, embedding, _, _ = _make_service()
        item = ChecklistItem(item_id="ci-1", title="Step A")
        task = _make_task(checklist_items=[item])

        await svc.index_task(task)

        calls = [str(c) for c in embedding.get_embedding.call_args_list]
        assert any("Step A" in c for c in calls)

    async def test_context_text_includes_list_name(self):
        svc, embedding, _, _ = _make_service()
        task = _make_task(list_name="Work")

        await svc.index_task(task)

        calls = [str(c) for c in embedding.get_embedding.call_args_list]
        assert any("Work" in c for c in calls)

    async def test_context_text_includes_tags(self):
        svc, embedding, _, _ = _make_service()
        task = _make_task(tags=["urgent"])

        await svc.index_task(task)

        calls = [str(c) for c in embedding.get_embedding.call_args_list]
        assert any("urgent" in c for c in calls)


# =============================================================================
# deindex_task
# =============================================================================


class TestDeindexTask:

    async def test_calls_delete(self):
        svc, _, index, _ = _make_service()

        await svc.deindex_task(_USER_ID, _TASK_ID)

        index.delete.assert_called_once_with(_USER_ID, _TASK_ID)


# =============================================================================
# index_task_by_ref
# =============================================================================


class TestIndexTaskByRef:

    async def test_fetches_then_indexes(self):
        svc, _, index, provider = _make_service()
        task = _make_task()
        provider.get_task.return_value = task

        await svc.index_task_by_ref(_USER_ID, _LIST_ID, _TASK_ID)

        provider.get_task.assert_called_once_with(_USER_ID, _LIST_ID, _TASK_ID)
        index.upsert.assert_called_once()


# =============================================================================
# reindex_list
# =============================================================================


class TestReindexList:

    async def test_indexes_all_tasks(self):
        svc, _, index, provider = _make_service()
        tasks = [_make_task(task_id=f"t{i}") for i in range(3)]
        provider.list_tasks.return_value = tasks

        await svc.reindex_list(_USER_ID, _LIST_ID)

        assert index.upsert.call_count == 3

    async def test_shows_completed(self):
        svc, _, _, provider = _make_service()
        provider.list_tasks.return_value = []

        await svc.reindex_list(_USER_ID, _LIST_ID)

        provider.list_tasks.assert_called_once_with(_USER_ID, _LIST_ID, show_completed=True)

    async def test_continues_on_single_task_error(self):
        svc, embedding, index, provider = _make_service()
        tasks = [_make_task(task_id=f"t{i}") for i in range(2)]
        provider.list_tasks.return_value = tasks
        # First embed call fails, rest succeed
        embedding.get_embedding.side_effect = [Exception("embed fail"), _VECTOR, _VECTOR, _VECTOR]

        await svc.reindex_list(_USER_ID, _LIST_ID)

        # One task failed, one succeeded → 1 upsert
        assert index.upsert.call_count == 1


# =============================================================================
# search
# =============================================================================


class TestSearch:

    async def test_embeds_query(self):
        svc, embedding, _, _ = _make_service()

        await svc.search(_USER_ID, "buy groceries")

        embedding.get_embedding.assert_called_once_with(
            "buy groceries", task_type="RETRIEVAL_QUERY"
        )

    async def test_calls_find_nearest(self):
        svc, _, index, _ = _make_service()

        await svc.search(_USER_ID, "buy groceries")

        index.find_nearest.assert_called_once()

    async def test_passes_both_vectors(self):
        svc, embedding, index, _ = _make_service()
        query_vector = [0.9, 0.8, 0.7]
        embedding.get_embedding.return_value = query_vector

        await svc.search(_USER_ID, "buy groceries")

        call_kwargs = index.find_nearest.call_args.kwargs
        vectors = call_kwargs.get("vectors", {})
        assert vectors.get("content_vector") == query_vector
        assert vectors.get("context_vector") == query_vector

    async def test_passes_show_completed(self):
        svc, _, index, _ = _make_service()

        await svc.search(_USER_ID, "done tasks", show_completed=True)

        call_kwargs = index.find_nearest.call_args.kwargs
        assert call_kwargs.get("show_completed") is True

    async def test_passes_list_id_filter(self):
        svc, _, index, _ = _make_service()

        await svc.search(_USER_ID, "task", list_id=_LIST_ID)

        call_kwargs = index.find_nearest.call_args.kwargs
        assert call_kwargs.get("list_id") == _LIST_ID
