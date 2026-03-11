"""
Unit tests for TasksAgent.

One intent: manage_user_tasks.
Agent uses a tool-calling loop — LLM autonomously selects list/search/create/update/delete.

Tests cover:
- can_handle: accepts QUERY with non-empty query; rejects DELEGATE, empty/missing query
- tool-calling loop: list, create, search→update (two turns), immediate text (no tool calls)
- tool execution errors: provider failure appends error dict, agent continues
- _execute_tool: direct dispatch to provider, correct arg mapping, unknown tool
- _format_task_list: dict structure (tool result, not final output)
- _parse_date: valid date, malformed date, None
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.tasks_agent import TasksAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.task import Task, TaskCreate, TaskStatus, TaskUpdate
from src.domain.user import PerformanceTier
from src.infrastructure.agent_manifest import Intent
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMResponse,
    ProviderCapabilities,
    ToolCall,
)
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.tasks_provider_port import TasksProviderPort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc123"
_ACCOUNT_ID = "account-xyz"


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="tasks",
        provider=mock_llm,
        model_name="gemini-flash-latest",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(native_tools=True),
    )


def _make_agent(mock_llm, mock_tasks) -> TasksAgent:
    mock_prompt_builder = AsyncMock(spec=PromptBuilderPort)
    mock_prompt_builder.build_for_agent.return_value = "system prompt"
    return TasksAgent(
        config=AgentConfig(
            agent_id=f"tasks_agent_{_USER_ID}",
            agent_type="tasks",
        ),
        execution_context=_make_execution_context(mock_llm),
        prompt_builder=mock_prompt_builder,
        tasks_provider=mock_tasks,
        user_id=_USER_ID,
    )


def _make_message(query: str = "show my tasks", intent: str = Intent.MANAGE_USER_TASKS) -> AgentMessage:
    return AgentMessage(
        task_id="task-1",
        intent=AgentIntent.QUERY,
        payload={"intent": intent, "query": query},
        context={"user_id": _USER_ID, "account_id": _ACCOUNT_ID},
        sender="quick_response_agent",
        recipient="tasks_agent",
    )


def _make_task(
    task_id: str = "t1",
    title: str = "Buy milk",
    status: TaskStatus = TaskStatus.NEEDS_ACTION,
    due_date: datetime | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        title=title,
        status=status,
        due_date=due_date,
        provider="google_tasks",
    )


def _tool_call_response(name: str, args: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(name=name, args=args)])


def _text_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[])


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:
    @pytest.fixture
    def agent(self):
        return _make_agent(AsyncMock(spec=LLMPort), AsyncMock(spec=TasksProviderPort))

    async def test_accepts_query_with_non_empty_query(self, agent):
        msg = _make_message("show my tasks")
        assert await agent.can_handle(msg) is True

    async def test_rejects_non_query_intent(self, agent):
        msg = AgentMessage(
            task_id="t",
            intent=AgentIntent.DELEGATE,
            payload={"intent": Intent.MANAGE_USER_TASKS, "query": "show tasks"},
            context={},
            sender="quick_response_agent",
            recipient="tasks_agent",
        )
        assert await agent.can_handle(msg) is False

    async def test_rejects_empty_query(self, agent):
        msg = _make_message(query="")
        assert await agent.can_handle(msg) is False

    async def test_rejects_missing_query(self, agent):
        msg = AgentMessage(
            task_id="t",
            intent=AgentIntent.QUERY,
            payload={"intent": Intent.MANAGE_USER_TASKS},
            context={},
            sender="quick_response_agent",
            recipient="tasks_agent",
        )
        assert await agent.can_handle(msg) is False


# ---------------------------------------------------------------------------
# Tool-calling loop
# ---------------------------------------------------------------------------


class TestToolCallingLoop:
    @pytest.fixture
    def mock_llm(self):
        return AsyncMock(spec=LLMPort)

    @pytest.fixture
    def mock_tasks(self):
        return AsyncMock(spec=TasksProviderPort)

    @pytest.fixture
    def agent(self, mock_llm, mock_tasks):
        return _make_agent(mock_llm, mock_tasks)

    async def test_list_tasks_tool_call_then_text(self, agent, mock_llm, mock_tasks):
        """LLM calls list_tasks → provider returns tasks → LLM formats text → SUCCESS."""
        mock_tasks.list_tasks.return_value = [_make_task("t1", "Buy milk")]
        mock_llm.generate_content.side_effect = [
            _tool_call_response("list_tasks", {"show_completed": False}),
            _text_response("You have 1 task: Buy milk"),
        ]

        response = await agent.execute(_make_message("show my tasks"))

        assert response.status == AgentStatus.SUCCESS
        assert response.result == "You have 1 task: Buy milk"
        mock_tasks.list_tasks.assert_called_once_with(user_id=_USER_ID, show_completed=False)

    async def test_create_task_tool_call(self, agent, mock_llm, mock_tasks):
        """LLM calls create_task → provider creates → LLM confirms → SUCCESS."""
        created = _make_task("new-id", "Buy milk")
        mock_tasks.create_task.return_value = created
        mock_llm.generate_content.side_effect = [
            _tool_call_response("create_task", {"title": "Buy milk"}),
            _text_response("Task 'Buy milk' created."),
        ]

        response = await agent.execute(_make_message("add task buy milk"))

        assert response.status == AgentStatus.SUCCESS
        mock_tasks.create_task.assert_called_once()
        call_task: TaskCreate = mock_tasks.create_task.call_args.kwargs["task"]
        assert call_task.title == "Buy milk"

    async def test_search_then_update_two_turns(self, agent, mock_llm, mock_tasks):
        """LLM calls search_tasks on turn 1, update_task on turn 2 — two tool turns."""
        mock_tasks.search_tasks.return_value = [_make_task("t1", "Buy milk")]
        mock_tasks.update_task.return_value = _make_task("t1", status=TaskStatus.COMPLETED)
        mock_llm.generate_content.side_effect = [
            _tool_call_response("search_tasks", {"query": "milk"}),
            _tool_call_response("update_task", {"task_id": "t1", "status": "completed"}),
            _text_response("Task marked as done."),
        ]

        response = await agent.execute(_make_message("mark buy milk as done"))

        assert response.status == AgentStatus.SUCCESS
        mock_tasks.search_tasks.assert_called_once_with(user_id=_USER_ID, query="milk")
        mock_tasks.update_task.assert_called_once()
        update_args = mock_tasks.update_task.call_args.kwargs
        assert update_args["task_id"] == "t1"
        assert update_args["updates"].status == TaskStatus.COMPLETED

    async def test_immediate_text_no_tool_calls(self, agent, mock_llm, mock_tasks):
        """LLM returns text immediately without any tool call → SUCCESS, no provider calls."""
        mock_llm.generate_content.return_value = _text_response("Here is what I found...")

        response = await agent.execute(_make_message("what should I do?"))

        assert response.status == AgentStatus.SUCCESS
        mock_tasks.list_tasks.assert_not_called()
        mock_tasks.search_tasks.assert_not_called()

    async def test_provider_error_appended_as_error_dict(self, agent, mock_llm, mock_tasks):
        """Provider failure → error dict appended to messages → LLM continues."""
        mock_tasks.list_tasks.side_effect = ValueError("No credentials")
        mock_llm.generate_content.side_effect = [
            _tool_call_response("list_tasks", {}),
            _text_response("Could not access your tasks."),
        ]

        response = await agent.execute(_make_message("show tasks"))

        assert response.status == AgentStatus.SUCCESS
        # LLM was called twice: once tool call, once after error dict
        assert mock_llm.generate_content.call_count == 2

    async def test_no_final_text_returns_failure(self, agent, mock_llm, mock_tasks):
        """LLM returns empty text and no tool calls → FAILED."""
        mock_llm.generate_content.return_value = LLMResponse(text="", tool_calls=[])

        response = await agent.execute(_make_message("do something"))

        assert response.status == AgentStatus.FAILED


# ---------------------------------------------------------------------------
# _execute_tool dispatch
# ---------------------------------------------------------------------------


class TestExecuteTool:
    @pytest.fixture
    def mock_tasks(self):
        return AsyncMock(spec=TasksProviderPort)

    @pytest.fixture
    def agent(self, mock_tasks):
        return _make_agent(AsyncMock(spec=LLMPort), mock_tasks)

    async def test_list_tasks_with_show_completed(self, agent, mock_tasks):
        mock_tasks.list_tasks.return_value = []
        await agent._execute_tool("list_tasks", {"show_completed": True}, _USER_ID)
        mock_tasks.list_tasks.assert_called_once_with(user_id=_USER_ID, show_completed=True)

    async def test_create_task_parses_due_date(self, agent, mock_tasks):
        created = _make_task("t1", "Pay bills", due_date=datetime(2026, 3, 15))
        mock_tasks.create_task.return_value = created
        await agent._execute_tool(
            "create_task", {"title": "Pay bills", "due_date": "2026-03-15"}, _USER_ID
        )
        call_task: TaskCreate = mock_tasks.create_task.call_args.kwargs["task"]
        assert call_task.title == "Pay bills"
        assert call_task.due_date == datetime(2026, 3, 15)

    async def test_update_task_completed_status(self, agent, mock_tasks):
        mock_tasks.update_task.return_value = _make_task("t1", status=TaskStatus.COMPLETED)
        await agent._execute_tool(
            "update_task", {"task_id": "t1", "status": "completed"}, _USER_ID
        )
        update_args = mock_tasks.update_task.call_args.kwargs
        assert update_args["task_id"] == "t1"
        assert update_args["updates"].status == TaskStatus.COMPLETED

    async def test_delete_task(self, agent, mock_tasks):
        mock_tasks.delete_task.return_value = None
        result = await agent._execute_tool("delete_task", {"task_id": "t1"}, _USER_ID)
        mock_tasks.delete_task.assert_called_once_with(user_id=_USER_ID, task_id="t1")
        assert result["deleted"] is True

    async def test_unknown_tool_returns_error_dict(self, agent, mock_tasks):
        result = await agent._execute_tool("fly_to_moon", {}, _USER_ID)
        assert "error" in result
        assert "fly_to_moon" in result["error"]


# ---------------------------------------------------------------------------
# _format_task_list (tool result structure, not final LLM output)
# ---------------------------------------------------------------------------


class TestFormatTaskList:
    def test_non_empty_list_structure(self):
        tasks = [_make_task("t1", "Buy milk"), _make_task("t2", "Read book")]
        result = TasksAgent._format_task_list(tasks)
        assert result["count"] == 2
        assert result["tasks"][0]["task_id"] == "t1"
        assert result["tasks"][0]["title"] == "Buy milk"
        assert result["tasks"][0]["status"] == "needsAction"

    def test_task_with_due_date(self):
        task = _make_task("t1", "Pay bills", due_date=datetime(2026, 3, 15))
        result = TasksAgent._format_task_list([task])
        assert result["tasks"][0]["due_date"] == "2026-03-15"

    def test_empty_list(self):
        result = TasksAgent._format_task_list([])
        assert result["tasks"] == []
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_valid_date(self):
        result = TasksAgent._parse_date("2026-03-15")
        assert result == datetime(2026, 3, 15)

    def test_datetime_string_truncated(self):
        result = TasksAgent._parse_date("2026-03-15T10:00:00Z")
        assert result == datetime(2026, 3, 15)

    def test_malformed_returns_none(self):
        assert TasksAgent._parse_date("not-a-date") is None

    def test_none_returns_none(self):
        assert TasksAgent._parse_date(None) is None

    def test_empty_string_returns_none(self):
        assert TasksAgent._parse_date("") is None
