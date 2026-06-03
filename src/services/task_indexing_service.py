"""
TaskIndexingService — embed → index pipeline for MS To Do tasks.
See docs/10_rfcs/TASKS_LOCAL_FIRST_RFC.md §7.2.

No port needed — single implementation.
Used by: TasksAgent (CRUD), webhook handler, WorkerHandler (reindex).
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from ..domain.task import Task, TaskSearchEntry
from ..ports.embedding_service import EmbeddingService
from ..ports.task_search_index import TaskSearchIndex
from ..ports.tasks_provider_port import TasksProviderPort
from ..utils.logger import logger

_REINDEX_CONCURRENCY = 5


class TaskIndexingService:
    """Encapsulates the embed→index pipeline for task search."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        search_index: TaskSearchIndex,
        tasks_provider: TasksProviderPort,
    ) -> None:
        self._embedding = embedding_service
        self._index = search_index
        self._tasks_provider = tasks_provider

    # ------------------------------------------------------------------
    # index_task
    # ------------------------------------------------------------------

    async def index_task(self, task: Task) -> None:
        """
        Embed and upsert a single task into the search index.

        content_vector: title + body + checklist items text
        context_vector: list_name + tags + importance
        """
        content_text = self._content_text(task)
        context_text = self._context_text(task)

        content_vector, context_vector = await asyncio.gather(
            self._embedding.get_embedding(content_text, task_type="RETRIEVAL_DOCUMENT"),
            self._embedding.get_embedding(context_text, task_type="RETRIEVAL_DOCUMENT"),
        )

        short_id = hashlib.md5(task.task_id.encode()).hexdigest()[:8]
        entry = TaskSearchEntry(
            task_id=task.task_id,
            list_id=task.list_id,
            list_name=task.list_name,
            user_id=task.user_id,
            title=task.title,
            status=task.status,
            tags=list(task.tags),
            importance=task.importance,
            short_id=short_id,
            content_vector=content_vector,
            context_vector=context_vector,
            indexed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

        await self._index.upsert(entry)
        logger.debug(f"🔍 Indexed task {task.task_id[:8]} for user {task.user_id[:8]}")

    # ------------------------------------------------------------------
    # deindex_task
    # ------------------------------------------------------------------

    async def deindex_task(self, user_id: str, task_id: str) -> None:
        """Remove a task from the search index."""
        await self._index.delete(user_id, task_id)
        logger.debug(f"🗑️ Deindexed task {task_id[:8]} for user {user_id[:8]}")

    # ------------------------------------------------------------------
    # index_task_by_ref
    # ------------------------------------------------------------------

    async def index_task_by_ref(self, user_id: str, list_id: str, task_id: str) -> None:
        """
        Fetch task from provider then index it.
        Used by webhook handler on created/updated notifications.
        """
        task = await self._tasks_provider.get_task(user_id, list_id, task_id)
        await self.index_task(task)

    # ------------------------------------------------------------------
    # reindex_list
    # ------------------------------------------------------------------

    async def reindex_list(self, user_id: str, list_id: str) -> None:
        """
        Re-index all tasks in a list (including completed).
        Bounded concurrency = 5. Used by WorkerHandler reindex_task_list.
        """
        tasks = await self._tasks_provider.list_tasks(user_id, list_id, show_completed=True)
        semaphore = asyncio.Semaphore(_REINDEX_CONCURRENCY)

        async def _index_one(task: Task) -> None:
            async with semaphore:
                try:
                    await self.index_task(task)
                except Exception as e:
                    logger.error(
                        f"❌ Failed to index task {task.task_id[:8]}: {e}", exc_info=True
                    )

        await asyncio.gather(*[_index_one(t) for t in tasks])
        logger.info(
            f"✅ Reindexed {len(tasks)} tasks for list {list_id[:8]}, user {user_id[:8]}"
        )

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    async def search(
        self,
        user_id: str,
        query: str,
        show_completed: bool = False,
        list_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[TaskSearchEntry]:
        """
        Embed query and find nearest tasks.
        Same query vector used for both content and context fields.
        Used by TasksAgent search_tasks tool.
        """
        query_vector = await self._embedding.get_embedding(
            query, task_type="RETRIEVAL_QUERY"
        )
        return await self._index.find_nearest(
            user_id=user_id,
            vectors={"content_vector": query_vector, "context_vector": query_vector},
            limit=limit,
            show_completed=show_completed,
            list_id=list_id,
        )

    # ------------------------------------------------------------------
    # resolve_short_id
    # ------------------------------------------------------------------

    async def resolve_short_id(self, user_id: str, short_id: str) -> Tuple[str, str]:
        """
        Resolve a short_id back to (list_id, task_id).
        Raises ValueError if not found.
        """
        entry = await self._index.get_by_short_id(user_id, short_id)
        if entry is None:
            raise ValueError(f"Task ref '{short_id}' not found in index for user {user_id[:8]}")
        return entry.list_id, entry.task_id

    # ------------------------------------------------------------------
    # Text builders
    # ------------------------------------------------------------------

    @staticmethod
    def _content_text(task: Task) -> str:
        parts = [task.title]
        if task.body:
            parts.append(task.body)
        for item in task.checklist_items:
            parts.append(item.title)
        return " ".join(parts)

    @staticmethod
    def _context_text(task: Task) -> str:
        parts = [task.list_name]
        parts.extend(task.tags)
        if task.importance:
            parts.append(task.importance.value)
        return " ".join(parts)
