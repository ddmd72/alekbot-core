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


# =============================================================================
# can_handle()
# =============================================================================


class TestCanHandle:

    async def test_query_intent_with_query_returns_true(self):
        from src.agents.tasks_agent import TasksAgent
        agent, _, _ = _make_agent()
        msg = _make_message("list tasks")
        assert await agent.can_handle(msg) is True

    async def test_delegate_intent_returns_false(self):
        agent, _, _ = _make_agent()
        msg = _make_message("list tasks")
        msg.intent = AgentIntent.DELEGATE
        # Rebuild as MagicMock to allow mutation
        m = MagicMock(spec=AgentMessage)
        m.intent = AgentIntent.DELEGATE
        m.payload = {"query": "list tasks"}
        assert await agent.can_handle(m) is False

    async def test_empty_query_returns_false(self):
        agent, _, _ = _make_agent()
        m = MagicMock(spec=AgentMessage)
        m.intent = AgentIntent.QUERY
        m.payload = {"query": ""}
        assert await agent.can_handle(m) is False


# =============================================================================
# execute() — edge cases
# =============================================================================


class TestExecuteEdgeCases:

    async def test_prompt_build_failure_still_succeeds(self):
        """Prompt build exception is swallowed; agent proceeds with empty system prompt."""
        from src.agents.tasks_agent import TasksAgent

        agent, provider, indexing = _make_agent()
        agent.prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore down")
        provider.list_tasks.return_value = []

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("list_tasks", {}, "Nothing found")
            response = await agent.execute(_make_message("list my tasks"))

        from src.domain.agent import AgentStatus
        assert response.status == AgentStatus.SUCCESS

    async def test_reasoning_appended_when_context_in_payload(self):
        """payload['context'] is appended to user_text."""
        agent, provider, indexing = _make_agent()
        provider.list_tasks.return_value = []
        captured = []

        async def capture(req, turn=1):
            captured.append(req)
            resp = MagicMock(spec=LLMResponse)
            resp.tool_calls = []
            resp.text = "Done."
            resp.raw_content = None
            return resp

        agent._call_llm = capture

        msg = AgentMessage(
            task_id="t1",
            sender="orch",
            recipient="tasks_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "List tasks", "context": "User prefers morning"},
            context={"user_id": _USER_ID, "account_id": "acc-1"},
        )
        await agent.execute(msg)
        first_user_text = captured[0].messages[0].parts[0].text
        assert "Context: User prefers morning" in first_user_text

    async def test_tool_exception_captured_as_error_dict(self):
        """When tool raises, result is {"error": "..."} and loop continues."""
        agent, provider, indexing = _make_agent()
        provider.list_tasks.side_effect = RuntimeError("provider down")

        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = _llm_responses("list_tasks", {}, "Handled.")
            response = await agent.execute(_make_message("list tasks"))

        from src.domain.agent import AgentStatus
        assert response.status == AgentStatus.SUCCESS

    async def test_max_turns_exhausted_forces_format(self):
        """When tool calls fill all turns, an extra no-tools call is made."""
        from src.ports.llm_port import LLMResponse

        agent, provider, indexing = _make_agent()
        provider.list_tasks.return_value = []

        tool_resp = MagicMock(spec=LLMResponse)
        tool_resp.tool_calls = [ToolCall(name="list_tasks", args={}, id="tc")]
        tool_resp.text = ""
        tool_resp.raw_content = None

        final_resp = MagicMock(spec=LLMResponse)
        final_resp.tool_calls = []
        final_resp.text = "Summary after max turns."
        final_resp.raw_content = None

        # 6 tool responses (MAX_TURNS=6) + 1 forced format response
        with patch.object(agent, "_call_llm") as mock_call:
            mock_call.side_effect = [tool_resp] * 6 + [final_resp]
            response = await agent.execute(_make_message("list tasks"))

        from src.domain.agent import AgentStatus
        assert response.status == AgentStatus.SUCCESS
        assert "Summary" in response.result

    async def test_empty_final_text_returns_failure(self):
        agent, provider, indexing = _make_agent()

        with patch.object(agent, "_call_llm") as mock_call:
            empty_resp = MagicMock(spec=LLMResponse)
            empty_resp.tool_calls = []
            empty_resp.text = ""
            empty_resp.raw_content = None
            mock_call.return_value = empty_resp
            response = await agent.execute(_make_message("list tasks"))

        from src.domain.agent import AgentStatus
        assert response.status == AgentStatus.FAILED


# =============================================================================
# _execute_tool() — additional coverage
# =============================================================================


class TestExecuteToolAdditional:

    async def test_create_task_with_recurrence(self):
        from src.agents.tasks_agent import TasksAgent

        agent, provider, indexing = _make_agent()
        provider.create_task.return_value = _make_task(title="Daily standup")
        indexing.index_task.return_value = None

        result = await agent._execute_tool(
            "create_task",
            {
                "title": "Daily standup",
                "recurrence": {"pattern": "daily", "interval": 1},
                "due_datetime": "2026-04-01T09:00:00",
            },
            _USER_ID,
        )
        assert result == {"created": True, "title": "Daily standup"}

    async def test_update_task_auto_reminder_when_no_prior_due(self):
        """Auto-reminder fires when current task.due_datetime is None and new due is set."""
        agent, provider, indexing = _make_agent()
        current_task = _make_task(due_datetime=None)
        updated_task = _make_task(title="Buy milk")

        indexing.resolve_short_id.return_value = (_LIST_ID, _TASK_ID)
        provider.get_task.return_value = current_task
        provider.update_task.return_value = updated_task
        indexing.index_task.return_value = None

        result = await agent._execute_tool(
            "update_task",
            {
                "task_ref": _SHORT_ID,
                "due_datetime": "2026-04-01T09:00:00",
            },
            _USER_ID,
        )
        assert result["updated"] is True
        updates = provider.update_task.call_args.kwargs["updates"]
        assert updates.is_reminder_on is True

    async def test_update_task_with_recurrence(self):
        agent, provider, indexing = _make_agent()
        current_task = _make_task()
        updated_task = _make_task(title="Weekly review")

        indexing.resolve_short_id.return_value = (_LIST_ID, _TASK_ID)
        provider.get_task.return_value = current_task
        provider.update_task.return_value = updated_task
        indexing.index_task.return_value = None

        result = await agent._execute_tool(
            "update_task",
            {
                "task_ref": _SHORT_ID,
                "recurrence": {"pattern": "weekly"},
                "due_datetime": "2026-04-07T10:00:00",
            },
            _USER_ID,
        )
        assert result["updated"] is True

    async def test_unknown_tool_returns_error(self):
        agent, _, _ = _make_agent()
        result = await agent._execute_tool("fly_to_moon", {}, _USER_ID)
        assert "error" in result
        assert "fly_to_moon" in result["error"]


# =============================================================================
# _derive_reminder()
# =============================================================================


class TestDeriveReminder:

    def test_explicit_reminder_used_as_is(self):
        from src.agents.tasks_agent import TasksAgent

        explicit = datetime(2026, 3, 31, 20, 0)
        reminder_dt, is_on = TasksAgent._derive_reminder(explicit, None)
        assert reminder_dt == explicit
        assert is_on is True

    def test_due_only_auto_reminder_day_before_8pm(self):
        from src.agents.tasks_agent import TasksAgent

        due = datetime(2026, 4, 1, 9, 0)
        reminder_dt, is_on = TasksAgent._derive_reminder(None, due)
        assert reminder_dt == datetime(2026, 3, 31, 20, 0, 0)
        assert is_on is True

    def test_neither_returns_none_false(self):
        from src.agents.tasks_agent import TasksAgent

        reminder_dt, is_on = TasksAgent._derive_reminder(None, None)
        assert reminder_dt is None
        assert is_on is False


# =============================================================================
# _format_task_list() — optional fields
# =============================================================================


class TestFormatTaskListOptionalFields:

    def _agent(self):
        agent, _, _ = _make_agent()
        return agent

    def test_body_included_when_present(self):
        from src.domain.task import ChecklistItem, LinkedResource, TaskAttachment, TaskRecurrence, RecurrencePattern, RecurrenceRange

        agent = self._agent()
        task = _make_task(body="Pick up 2 liters")
        result = agent._format_task_list([task])
        assert result["tasks"][0]["body"] == "Pick up 2 liters"

    def test_due_datetime_included(self):
        agent = self._agent()
        due = datetime(2026, 4, 1, 9, 0)
        task = _make_task(due_datetime=due)
        result = agent._format_task_list([task])
        assert result["tasks"][0]["due_datetime"] == due.isoformat()

    def test_reminder_datetime_and_is_reminder_on_included(self):
        agent = self._agent()
        reminder = datetime(2026, 3, 31, 20, 0)
        task = _make_task(reminder_datetime=reminder, is_reminder_on=True)
        result = agent._format_task_list([task])
        d = result["tasks"][0]
        assert d["reminder_datetime"] == reminder.isoformat()
        assert d["is_reminder_on"] is True

    def test_completed_at_included(self):
        agent = self._agent()
        completed = datetime(2026, 3, 29, 14, 0)
        task = _make_task(completed_at=completed, status=TaskStatus.COMPLETED)
        result = agent._format_task_list([task])
        assert result["tasks"][0]["completed_at"] == completed.isoformat()

    def test_checklist_items_included(self):
        from src.domain.task import ChecklistItem

        agent = self._agent()
        item = ChecklistItem(item_id="ci-1", title="Step 1", is_completed=False)
        task = _make_task(checklist_items=[item])
        result = agent._format_task_list([task])
        ci = result["tasks"][0]["checklist_items"][0]
        assert ci["title"] == "Step 1"
        assert ci["is_completed"] is False

    def test_checklist_item_with_checked_at(self):
        from src.domain.task import ChecklistItem

        agent = self._agent()
        checked = datetime(2026, 3, 29, 10, 0)
        item = ChecklistItem(item_id="ci-1", title="Done", is_completed=True, checked_at=checked)
        task = _make_task(checklist_items=[item])
        result = agent._format_task_list([task])
        assert result["tasks"][0]["checklist_items"][0]["checked_at"] == checked.isoformat()

    def test_linked_resources_included(self):
        from src.domain.task import LinkedResource

        agent = self._agent()
        lr = LinkedResource(resource_id="r1", web_url="https://example.com", display_name="Doc")
        task = _make_task(linked_resources=[lr])
        result = agent._format_task_list([task])
        lr_out = result["tasks"][0]["linked_resources"][0]
        assert lr_out["web_url"] == "https://example.com"
        assert lr_out["display_name"] == "Doc"

    def test_linked_resource_with_application_name(self):
        from src.domain.task import LinkedResource

        agent = self._agent()
        lr = LinkedResource(
            resource_id="r1", web_url="https://x.com",
            display_name="X", application_name="MyApp"
        )
        task = _make_task(linked_resources=[lr])
        result = agent._format_task_list([task])
        assert result["tasks"][0]["linked_resources"][0]["application_name"] == "MyApp"

    def test_recurrence_included(self):
        from src.domain.task import TaskRecurrence, RecurrencePattern, RecurrenceRange

        agent = self._agent()
        rec = TaskRecurrence(
            pattern=RecurrencePattern(type="daily", interval=1),
            range=RecurrenceRange(type="noEnd", start_date="2026-04-01"),
        )
        task = _make_task(recurrence=rec)
        result = agent._format_task_list([task])
        r = result["tasks"][0]["recurrence"]
        assert r["type"] == "daily"
        assert r["interval"] == 1

    def test_attachments_included(self):
        from src.domain.task import TaskAttachment

        agent = self._agent()
        att = TaskAttachment(attachment_id="a1", filename="report.pdf", url="https://gcs/file")
        task = _make_task(attachments=[att])
        result = agent._format_task_list([task])
        a_out = result["tasks"][0]["attachments"][0]
        assert a_out["filename"] == "report.pdf"
        assert a_out["url"] == "https://gcs/file"

    def test_attachment_with_gcs_uri(self):
        from src.domain.task import TaskAttachment

        agent = self._agent()
        att = TaskAttachment(attachment_id="a1", filename="f.pdf", gcs_uri="gs://bucket/f.pdf")
        task = _make_task(attachments=[att])
        result = agent._format_task_list([task])
        assert result["tasks"][0]["attachments"][0]["gcs_uri"] == "gs://bucket/f.pdf"


# =============================================================================
# _parse_recurrence()
# =============================================================================


class TestParseRecurrence:

    def _agent(self):
        agent, _, _ = _make_agent()
        return agent

    def test_daily_pattern(self):
        agent = self._agent()
        rec = agent._parse_recurrence({"pattern": "daily", "interval": 2}, None)
        assert rec.pattern.type == "daily"
        assert rec.pattern.interval == 2

    def test_weekdays_aliases_to_weekly_mon_to_fri(self):
        agent = self._agent()
        rec = agent._parse_recurrence({"pattern": "weekdays"}, None)
        assert rec.pattern.type == "weekly"
        assert set(rec.pattern.days_of_week) == {
            "monday", "tuesday", "wednesday", "thursday", "friday"
        }

    def test_weekly_defaults_to_due_datetime_weekday(self):
        agent = self._agent()
        # 2026-04-01 is a Wednesday (weekday index 2)
        rec = agent._parse_recurrence({"pattern": "weekly"}, "2026-04-01T10:00:00")
        assert rec.pattern.type == "weekly"
        assert rec.pattern.days_of_week == ["wednesday"]

    def test_weekly_explicit_days_of_week(self):
        agent = self._agent()
        rec = agent._parse_recurrence(
            {"pattern": "weekly", "days_of_week": ["monday", "friday"]},
            "2026-04-01T10:00:00",
        )
        assert rec.pattern.days_of_week == ["monday", "friday"]

    def test_absolute_monthly_defaults_to_due_day(self):
        agent = self._agent()
        rec = agent._parse_recurrence({"pattern": "absoluteMonthly"}, "2026-04-15T10:00:00")
        assert rec.pattern.type == "absoluteMonthly"
        assert rec.pattern.day_of_month == 15

    def test_absolute_yearly_defaults_from_due(self):
        agent = self._agent()
        rec = agent._parse_recurrence({"pattern": "absoluteYearly"}, "2026-04-15T10:00:00")
        assert rec.pattern.type == "absoluteYearly"
        assert rec.pattern.day_of_month == 15
        assert rec.pattern.month == 4

    def test_range_is_no_end(self):
        agent = self._agent()
        rec = agent._parse_recurrence({"pattern": "daily"}, None)
        assert rec.range.type == "noEnd"


# =============================================================================
# _parse_datetime()
# =============================================================================


class TestParseDatetime:

    def test_none_returns_none(self):
        from src.agents.tasks_agent import TasksAgent
        assert TasksAgent._parse_datetime(None) is None

    def test_non_string_returns_none(self):
        from src.agents.tasks_agent import TasksAgent
        assert TasksAgent._parse_datetime(42) is None

    def test_valid_iso_returns_datetime(self):
        from src.agents.tasks_agent import TasksAgent
        result = TasksAgent._parse_datetime("2026-04-01T10:00:00")
        assert result == datetime(2026, 4, 1, 10, 0, 0)

    def test_trailing_z_stripped(self):
        from src.agents.tasks_agent import TasksAgent
        result = TasksAgent._parse_datetime("2026-04-01T10:00:00Z")
        assert result == datetime(2026, 4, 1, 10, 0, 0)

    def test_invalid_iso_returns_none(self):
        from src.agents.tasks_agent import TasksAgent
        result = TasksAgent._parse_datetime("not-a-date")
        assert result is None
