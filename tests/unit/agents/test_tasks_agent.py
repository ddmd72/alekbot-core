"""
Unit tests for TasksAgent.

Mock boundary: ports (TasksProviderPort, TaskIndexingService) and LLM.

Covers (per RFC §14):
- search_tasks: delegates to task_indexing.search -> batch_get_tasks
- create_task: calls tasks_provider.create_task -> task_indexing.index_task
- update_task: calls tasks_provider.update_task -> task_indexing.index_task
- delete_task: calls tasks_provider.delete_task -> task_indexing.deindex_task
- list_tasks: delegates to tasks_provider.list_tasks
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from src.domain.task import (
    Task,
    TaskImportance,
    TaskSearchEntry,
    TaskStatus,
)
from src.ports.llm_port import AgentExecutionContext, LLMResponse, ToolCall
from src.ports.tasks_provider_port import TasksProviderPort
from src.services.task_indexing_service import TaskIndexingService

_USER_ID = "user-1"
_LIST_ID = "list-1"
_TASK_ID = "task-1"
_SHORT_ID = "c146b6ad"  # hashlib.md5(_TASK_ID.encode()).hexdigest()[:8]


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


def _make_entry(**kwargs) -> TaskSearchEntry:
    defaults = dict(
        task_id=_TASK_ID,
        list_id=_LIST_ID,
        list_name="Alek Bot Tasks",
        user_id=_USER_ID,
        title="Buy milk",
        status=TaskStatus.NOT_STARTED,
        tags=["shopping"],
        importance=TaskImportance.NORMAL,
        indexed_at=datetime(2026, 3, 18),
    )
    defaults.update(kwargs)
    return TaskSearchEntry(**defaults)


def _make_agent():
    from src.agents.tasks_agent import TasksAgent

    provider = AsyncMock(spec=TasksProviderPort)
    indexing = AsyncMock(spec=TaskIndexingService)

    execution_context = MagicMock(spec=AgentExecutionContext)
    execution_context.provider = AsyncMock()
    execution_context.model_name = "test-model"

    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "system prompt"

    agent = TasksAgent(
        config=AgentConfig(
            agent_id=f"tasks_agent_{_USER_ID}",
            agent_type="tasks",
            timeout_ms=10000,
        ),
        execution_context=execution_context,
        prompt_builder=prompt_builder,
        tasks_provider=provider,
        task_indexing=indexing,
        user_id=_USER_ID,
    )

    return agent, provider, indexing


def _make_message(query: str = "find milk task") -> AgentMessage:
    return AgentMessage(
        task_id="task-abc",
        sender="quick_response_agent",
        recipient="tasks_agent",
        intent=AgentIntent.QUERY,
        payload={"query": query},
        context={"user_id": _USER_ID, "account_id": "acc-1"},
    )


def _llm_responses(tool_name: str, tool_args: dict, final_text: str):
    """Returns (tool_response, text_response) mocks for _call_llm side_effect."""
    tool_resp = MagicMock(spec=LLMResponse)
    tool_resp.tool_calls = [ToolCall(name=tool_name, args=tool_args, id="tc-1")]
    tool_resp.text = ""
    tool_resp.raw_content = None

    text_resp = MagicMock(spec=LLMResponse)
    text_resp.tool_calls = []
    text_resp.text = final_text
    text_resp.raw_content = None

    return [tool_resp, text_resp]


# =============================================================================
# search_tasks
# =============================================================================


class TestSearchTasks:

    async def test_search_delegates_to_indexing(self):
        agent, provider, indexing = _make_agent()
        indexing.search.return_value = [_make_entry()]
        provider.batch_get_tasks.return_value = [_make_task()]

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("search_tasks", {"query": "milk"}, "Found: Buy milk")
            await agent.execute(_make_message(query="find milk"))

        indexing.search.assert_called_once()

    async def test_search_passes_query(self):
        agent, provider, indexing = _make_agent()
        indexing.search.return_value = [_make_entry()]
        provider.batch_get_tasks.return_value = [_make_task()]

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("search_tasks", {"query": "milk"}, "Found")
            await agent.execute(_make_message(query="find milk"))

        indexing.search.assert_called_once_with(user_id=_USER_ID, query="milk", show_completed=False)

    async def test_search_then_batch_get(self):
        agent, provider, indexing = _make_agent()
        entry = _make_entry()
        indexing.search.return_value = [entry]
        provider.batch_get_tasks.return_value = [_make_task()]

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("search_tasks", {"query": "milk"}, "Found tasks")
            await agent.execute(_make_message(query="find milk"))

        provider.batch_get_tasks.assert_called_once()
        call_kwargs = provider.batch_get_tasks.call_args.kwargs
        assert (_LIST_ID, _TASK_ID) in call_kwargs["task_refs"]

    async def test_search_empty_skips_batch_get(self):
        agent, provider, indexing = _make_agent()
        indexing.search.return_value = []

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("search_tasks", {"query": "xyz"}, "Nothing found")
            await agent.execute(_make_message(query="xyz"))

        provider.batch_get_tasks.assert_not_called()


# =============================================================================
# create_task
# =============================================================================


class TestCreateTask:

    async def test_create_calls_provider(self):
        agent, provider, indexing = _make_agent()
        task = _make_task(title="Buy milk")
        provider.create_task.return_value = task

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("create_task", {"title": "Buy milk"}, "Created")
            await agent.execute(_make_message(query="create milk task"))

        provider.create_task.assert_called_once()

    async def test_create_indexes_after_create(self):
        agent, provider, indexing = _make_agent()
        task = _make_task(title="Buy milk")
        provider.create_task.return_value = task

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("create_task", {"title": "Buy milk"}, "Created")
            await agent.execute(_make_message(query="create milk task"))

        indexing.index_task.assert_called_once_with(task)


# =============================================================================
# update_task
# =============================================================================


class TestUpdateTask:

    async def test_update_calls_provider_with_list_id(self):
        agent, provider, indexing = _make_agent()
        updated_task = _make_task(title="Updated")
        provider.update_task.return_value = updated_task
        indexing.resolve_short_id.return_value = (_LIST_ID, _TASK_ID)

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses(
                "update_task",
                {"task_ref": _SHORT_ID, "title": "Updated"},
                "Updated",
            )
            await agent.execute(_make_message(query="rename task"))

        provider.update_task.assert_called_once()
        call_kwargs = provider.update_task.call_args.kwargs
        assert call_kwargs["task_id"] == _TASK_ID
        assert call_kwargs["list_id"] == _LIST_ID

    async def test_update_reindexes(self):
        agent, provider, indexing = _make_agent()
        updated_task = _make_task(status=TaskStatus.COMPLETED)
        provider.update_task.return_value = updated_task
        indexing.resolve_short_id.return_value = (_LIST_ID, _TASK_ID)

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses(
                "update_task",
                {"task_ref": _SHORT_ID, "status": "completed"},
                "Marked done",
            )
            await agent.execute(_make_message(query="mark task done"))

        indexing.index_task.assert_called_once_with(updated_task)


# =============================================================================
# delete_task
# =============================================================================


class TestDeleteTask:

    async def test_delete_calls_provider(self):
        agent, provider, indexing = _make_agent()
        provider.delete_task.return_value = None
        indexing.resolve_short_id.return_value = (_LIST_ID, _TASK_ID)

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses(
                "delete_task", {"task_ref": _SHORT_ID}, "Deleted"
            )
            await agent.execute(_make_message(query="delete milk task"))

        provider.delete_task.assert_called_once()
        call_kwargs = provider.delete_task.call_args.kwargs
        assert call_kwargs["task_id"] == _TASK_ID
        assert call_kwargs["list_id"] == _LIST_ID

    async def test_delete_deindexes(self):
        agent, provider, indexing = _make_agent()
        provider.delete_task.return_value = None
        indexing.resolve_short_id.return_value = (_LIST_ID, _TASK_ID)

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses(
                "delete_task", {"task_ref": _SHORT_ID}, "Deleted"
            )
            await agent.execute(_make_message(query="delete milk task"))

        indexing.deindex_task.assert_called_once_with(_USER_ID, _TASK_ID)


# =============================================================================
# list_tasks
# =============================================================================


class TestListTasks:

    async def test_list_calls_provider(self):
        agent, provider, _ = _make_agent()
        provider.list_tasks.return_value = [_make_task()]

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("list_tasks", {}, "Here are your tasks")
            await agent.execute(_make_message(query="list my tasks"))

        provider.list_tasks.assert_called_once()

    async def test_list_passes_show_completed(self):
        agent, provider, _ = _make_agent()
        provider.list_tasks.return_value = []

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses(
                "list_tasks", {"show_completed": True}, "Completed tasks"
            )
            await agent.execute(_make_message(query="show completed tasks"))

        call_kwargs = provider.list_tasks.call_args.kwargs
        assert call_kwargs.get("show_completed") is True
