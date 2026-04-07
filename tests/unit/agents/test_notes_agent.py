"""
Unit tests for NotesAgent.

Mock boundary: AgentNotePort and LLM (_call_llm patched).

Tests cover:
- can_handle: accepts manage_self_reminders, rejects other intents and non-QUERY
- create_self_reminder: LLM selects tool, port called, result returned
- update_self_reminder: LLM selects tool, port called with str note_id
- delete_self_reminder: LLM selects tool, port called
- delete not found: port returns False → AgentResponse.FAILED
- note_id int coercion: int note_id coerced to str before port call
- no tool call from LLM: FAILED response
- unknown tool: _execute_tool returns error dict without raising
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.notes_agent import NotesAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.agent_note import AgentNote, NoteCreate, NoteUpdate
from src.infrastructure.agent_manifest import Intent
from src.ports.agent_note_port import AgentNotePort
from src.ports.llm_port import AgentExecutionContext, LLMResponse, ToolCall
from src.ports.prompt_builder_port import PromptBuilderPort

_USER_ID = "user-abc123"
_NOTE_ID = "1741525822123"
_DUE_DT = datetime(2026, 3, 10, 9, 0, 0, tzinfo=timezone.utc)


def _make_note(note_id: str = _NOTE_ID, text: str = "Remind about dentist") -> AgentNote:
    return AgentNote(
        note_id=note_id,
        user_id=_USER_ID,
        text=text,
        instruction=text,
        created_at=datetime(2026, 3, 9, 14, 30, 22, tzinfo=timezone.utc),
        due=_DUE_DT,
    )


def _make_agent():
    port = AsyncMock(spec=AgentNotePort)
    port.list_active_notes.return_value = []

    execution_context = MagicMock(spec=AgentExecutionContext)
    execution_context.provider = AsyncMock()
    execution_context.model_name = "test-model"

    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = ""

    agent = NotesAgent(
        config=AgentConfig(
            agent_id=f"notes_agent_{_USER_ID}",
            agent_type="notes",
            timeout_ms=10_000,
            capabilities=["note_management"],
        ),
        execution_context=execution_context,
        notes_port=port,
        prompt_builder=prompt_builder,
    )
    return agent, port


def _make_message(query: str = "create a reminder", intent: str = Intent.MANAGE_SELF_REMINDERS) -> AgentMessage:
    return AgentMessage.create(
        sender="quick_response_agent",
        recipient=f"notes_agent_{_USER_ID}",
        intent=AgentIntent.QUERY,
        payload={"intent": intent, "query": query},
        context={"user_id": _USER_ID},
    )


def _tool_response(tool_name: str, tool_args: dict) -> LLMResponse:
    resp = MagicMock(spec=LLMResponse)
    resp.tool_calls = [ToolCall(name=tool_name, args=tool_args, id="tc-1")]
    resp.text = ""
    resp.raw_content = None
    return resp


def _no_tool_response() -> LLMResponse:
    resp = MagicMock(spec=LLMResponse)
    resp.tool_calls = []
    resp.text = "I'm not sure what to do."
    resp.raw_content = None
    return resp


# =============================================================================
# can_handle
# =============================================================================


class TestCanHandle:

    async def test_accepts_manage_self_reminders(self):
        agent, _ = _make_agent()
        msg = _make_message()
        assert await agent.can_handle(msg) is True

    async def test_rejects_unknown_intent(self):
        agent, _ = _make_agent()
        msg = _make_message(intent="search_memory")
        assert await agent.can_handle(msg) is False

    async def test_rejects_non_query_message_intent(self):
        agent, _ = _make_agent()
        msg = AgentMessage.create(
            sender="router",
            recipient="notes_agent",
            intent=AgentIntent.INFORM,
            payload={"intent": Intent.MANAGE_SELF_REMINDERS},
            context={"user_id": _USER_ID},
        )
        assert await agent.can_handle(msg) is False


# =============================================================================
# create_self_reminder
# =============================================================================


class TestCreateSelfReminder:

    async def test_create_calls_port(self):
        agent, port = _make_agent()
        port.create_note.return_value = _make_note()

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "create_self_reminder", {"text": "Remind about dentist", "due": "2026-03-10T09:00:00+00:00"}
        )):
            response = await agent.execute(_make_message("remind me about dentist tomorrow at 9am"))

        assert response.status == AgentStatus.SUCCESS
        port.create_note.assert_called_once()

    async def test_create_passes_text_to_port(self):
        agent, port = _make_agent()
        port.create_note.return_value = _make_note(text="Prague hotel booked")

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "create_self_reminder", {"text": "Prague hotel booked", "due": "2026-03-10T09:00:00+00:00"}
        )):
            await agent.execute(_make_message())

        call_arg: NoteCreate = port.create_note.call_args[0][0]
        assert call_arg.text == "Prague hotel booked"
        assert call_arg.user_id == _USER_ID

    async def test_create_result_is_success(self):
        agent, port = _make_agent()
        port.create_note.return_value = _make_note()

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "create_self_reminder", {"text": "Note text", "due": "2026-03-10T09:00:00+00:00"}
        )):
            response = await agent.execute(_make_message())

        assert response.status == AgentStatus.SUCCESS

    async def test_create_missing_due_returns_failure(self):
        agent, port = _make_agent()

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "create_self_reminder", {"text": "Remind about dentist"}  # no due
        )):
            response = await agent.execute(_make_message())

        assert response.status == AgentStatus.FAILED
        port.create_note.assert_not_called()


# =============================================================================
# update_self_reminder
# =============================================================================


class TestUpdateSelfReminder:

    async def test_update_calls_port(self):
        agent, port = _make_agent()
        port.update_note.return_value = _make_note(text="Updated text")

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "update_self_reminder", {"note_id": _NOTE_ID, "text": "Updated text"}
        )):
            response = await agent.execute(_make_message(f"update reminder {_NOTE_ID}"))

        assert response.status == AgentStatus.SUCCESS
        port.update_note.assert_called_once()

    async def test_update_passes_correct_data_to_port(self):
        agent, port = _make_agent()
        port.update_note.return_value = _make_note()

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "update_self_reminder", {"note_id": _NOTE_ID, "text": "New text"}
        )):
            await agent.execute(_make_message())

        call_arg: NoteUpdate = port.update_note.call_args[0][0]
        assert call_arg.note_id == _NOTE_ID
        assert call_arg.user_id == _USER_ID
        assert call_arg.text == "New text"

    async def test_update_coerces_int_note_id_to_str(self):
        agent, port = _make_agent()
        port.update_note.return_value = _make_note()

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "update_self_reminder", {"note_id": 1741525822123, "text": "Fixed"}
        )):
            await agent.execute(_make_message())

        call_arg: NoteUpdate = port.update_note.call_args[0][0]
        assert isinstance(call_arg.note_id, str)
        assert call_arg.note_id == "1741525822123"


# =============================================================================
# delete_self_reminder
# =============================================================================


class TestDeleteSelfReminder:

    async def test_delete_calls_port(self):
        agent, port = _make_agent()
        port.delete_note.return_value = True

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "delete_self_reminder", {"note_id": _NOTE_ID}
        )):
            response = await agent.execute(_make_message(f"delete reminder {_NOTE_ID}"))

        assert response.status == AgentStatus.SUCCESS
        port.delete_note.assert_called_once_with(note_id=_NOTE_ID, user_id=_USER_ID)

    async def test_delete_coerces_int_note_id_to_str(self):
        agent, port = _make_agent()
        port.delete_note.return_value = True

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "delete_self_reminder", {"note_id": 1741525822123}
        )):
            await agent.execute(_make_message())

        call_kwargs = port.delete_note.call_args.kwargs
        assert isinstance(call_kwargs["note_id"], str)
        assert call_kwargs["note_id"] == "1741525822123"

    async def test_delete_not_found_returns_failure(self):
        agent, port = _make_agent()
        port.delete_note.return_value = False

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "delete_self_reminder", {"note_id": "nonexistent"}
        )):
            response = await agent.execute(_make_message())

        assert response.status == AgentStatus.FAILED


# =============================================================================
# LLM returns no tool call
# =============================================================================


class TestNoToolCall:

    async def test_no_tool_call_returns_text_response(self):
        """LLM returning text without tool calls is valid — error messages, clarifications."""
        agent, _ = _make_agent()

        with patch.object(agent, "_call_llm", return_value=_no_tool_response()):
            response = await agent.execute(_make_message("do something unclear"))

        assert response.status == AgentStatus.SUCCESS


# =============================================================================
# _execute_tool — unknown tool name
# =============================================================================


class TestExecuteTool:

    async def test_unknown_tool_returns_error_dict(self):
        agent, _ = _make_agent()
        result = await agent._execute_tool("unknown_tool", {}, _USER_ID, _USER_ID)
        assert "error" in result
        assert "unknown_tool" in result["error"]


# =============================================================================
# _resolve_tz
# =============================================================================


class TestResolveTz:

    def test_none_input_returns_utc(self):
        from src.agents.notes_agent import _resolve_tz
        from zoneinfo import ZoneInfo

        result = _resolve_tz(None)
        assert result == ZoneInfo("UTC")

    def test_empty_string_returns_utc(self):
        from src.agents.notes_agent import _resolve_tz
        from zoneinfo import ZoneInfo

        result = _resolve_tz("")
        assert result == ZoneInfo("UTC")

    def test_invalid_timezone_returns_utc(self):
        from src.agents.notes_agent import _resolve_tz
        from zoneinfo import ZoneInfo

        result = _resolve_tz("Invalid/TZ")
        assert result == ZoneInfo("UTC")

    def test_valid_timezone_returned(self):
        from src.agents.notes_agent import _resolve_tz
        from zoneinfo import ZoneInfo

        result = _resolve_tz("Europe/Madrid")
        assert result == ZoneInfo("Europe/Madrid")


# =============================================================================
# _parse_dt
# =============================================================================


class TestParseDt:

    def test_naive_datetime_gets_user_tz(self):
        from src.agents.notes_agent import _parse_dt
        from zoneinfo import ZoneInfo

        user_tz = ZoneInfo("Europe/Madrid")
        # naive datetime — no tzinfo
        result = _parse_dt("2026-03-01T10:00:00", user_tz)

        assert result is not None
        assert result.tzinfo == timezone.utc
        # 10:00 Madrid in March (UTC+1) → 09:00 UTC
        assert result.hour == 9

    def test_invalid_string_returns_none(self):
        from src.agents.notes_agent import _parse_dt
        from zoneinfo import ZoneInfo

        user_tz = ZoneInfo("UTC")
        result = _parse_dt("not-a-date", user_tz)
        assert result is None

    def test_none_input_returns_none(self):
        from src.agents.notes_agent import _parse_dt
        from zoneinfo import ZoneInfo

        result = _parse_dt(None, ZoneInfo("UTC"))
        assert result is None

    def test_tz_aware_datetime_converted_to_utc(self):
        from src.agents.notes_agent import _parse_dt
        from zoneinfo import ZoneInfo

        user_tz = ZoneInfo("UTC")
        result = _parse_dt("2026-03-10T09:00:00+00:00", user_tz)
        assert result is not None
        assert result.tzinfo == timezone.utc


# =============================================================================
# _parse_recurrence
# =============================================================================


class TestParseRecurrence:

    def test_valid_recurrence_returns_object(self):
        from src.agents.notes_agent import _parse_recurrence
        from src.domain.agent_note import ReminderRecurrence

        result = _parse_recurrence({"type": "daily", "interval": 1})
        assert isinstance(result, ReminderRecurrence)
        assert result.type == "daily"
        assert result.interval == 1

    def test_none_input_returns_none(self):
        from src.agents.notes_agent import _parse_recurrence

        assert _parse_recurrence(None) is None

    def test_missing_type_returns_none(self):
        from src.agents.notes_agent import _parse_recurrence

        assert _parse_recurrence({"interval": 2}) is None

    def test_default_interval_is_one(self):
        from src.agents.notes_agent import _parse_recurrence

        result = _parse_recurrence({"type": "weekly"})
        assert result is not None
        assert result.interval == 1


# =============================================================================
# _run — missing prompt_builder raises
# =============================================================================


class TestRunMissingPromptBuilder:

    async def test_raises_when_no_prompt_builder(self):
        port = AsyncMock(spec=AgentNotePort)
        execution_context = MagicMock(spec=AgentExecutionContext)
        execution_context.provider = AsyncMock()
        execution_context.model_name = "test-model"

        agent = NotesAgent(
            config=AgentConfig(
                agent_id=f"notes_agent_{_USER_ID}",
                agent_type="notes",
                timeout_ms=10_000,
                capabilities=["note_management"],
            ),
            execution_context=execution_context,
            notes_port=port,
            prompt_builder=None,  # explicitly no prompt_builder
        )

        with pytest.raises(ValueError, match="NotesAgent requires prompt_builder"):
            await agent._run("remind me of something", _USER_ID, _USER_ID)


# =============================================================================
# _run — active_notes formatting appended to system_prompt
# =============================================================================


class TestRunActiveNotesFormatting:

    async def test_active_notes_block_appended_to_system_prompt(self):
        port = AsyncMock(spec=AgentNotePort)
        note = _make_note()
        # list_active_notes is called twice: once in _run, once after create in _execute_tool
        port.list_active_notes.return_value = [note]
        port.create_note.return_value = note

        execution_context = MagicMock(spec=AgentExecutionContext)
        execution_context.provider = AsyncMock()
        execution_context.model_name = "test-model"

        prompt_builder = AsyncMock(spec=PromptBuilderPort)
        prompt_builder.build_for_agent.return_value = "BASE_PROMPT"

        agent = NotesAgent(
            config=AgentConfig(
                agent_id=f"notes_agent_{_USER_ID}",
                agent_type="notes",
                timeout_ms=10_000,
                capabilities=["note_management"],
            ),
            execution_context=execution_context,
            notes_port=port,
            prompt_builder=prompt_builder,
        )

        captured_request = []

        async def fake_call_llm(request, turn):
            captured_request.append(request)
            resp = MagicMock()
            resp.tool_calls = []
            resp.text = "ok"
            resp.raw_content = None
            return resp

        with patch.object(agent, "_call_llm", side_effect=fake_call_llm):
            await agent._run("do something", _USER_ID, _USER_ID)

        assert len(captured_request) == 1
        system_instr = captured_request[0].system_instruction
        assert "active_reminders {" in system_instr
        assert note.note_id in system_instr
        assert note.text in system_instr
        assert note.instruction in system_instr


# =============================================================================
# _execute_tool create — soft threshold alert
# =============================================================================


class TestCreateSoftThresholdAlert:

    async def test_alert_set_when_at_soft_threshold(self):
        from src.agents.notes_agent import _NOTES_SOFT_THRESHOLD

        agent, port = _make_agent()
        note = _make_note()
        port.create_note.return_value = note
        # Return exactly _NOTES_SOFT_THRESHOLD notes so len(active) >= threshold
        port.list_active_notes.return_value = [_make_note(note_id=str(i)) for i in range(_NOTES_SOFT_THRESHOLD)]

        with patch.object(agent, "_call_llm", return_value=_tool_response(
            "create_self_reminder",
            {"text": "Remind about dentist", "instruction": "Call dentist", "due": "2026-03-10T09:00:00+00:00"},
        )):
            response = await agent.execute(_make_message("remind me about dentist"))

        assert response.status == AgentStatus.SUCCESS
        # Verify alert was set: re-run _execute_tool directly
        port.list_active_notes.return_value = [_make_note(note_id=str(i)) for i in range(_NOTES_SOFT_THRESHOLD)]
        result = await agent._execute_tool(
            "create_self_reminder",
            {"text": "Another reminder", "instruction": "Do it", "due": "2026-03-10T09:00:00+00:00"},
            _USER_ID,
            _USER_ID,
        )
        assert "alert" in result

    async def test_no_alert_below_soft_threshold(self):
        from src.agents.notes_agent import _NOTES_SOFT_THRESHOLD

        agent, port = _make_agent()
        note = _make_note()
        port.create_note.return_value = note
        # Return fewer notes than threshold
        port.list_active_notes.return_value = [_make_note(note_id=str(i)) for i in range(_NOTES_SOFT_THRESHOLD - 1)]

        result = await agent._execute_tool(
            "create_self_reminder",
            {"text": "Single reminder", "instruction": "Do it", "due": "2026-03-10T09:00:00+00:00"},
            _USER_ID,
            _USER_ID,
        )
        assert "alert" not in result


# =============================================================================
# _notify — no notification_service and exception swallowing
# =============================================================================


class TestNotify:

    async def test_returns_early_when_no_notification_service(self):
        agent, _ = _make_agent()
        # _make_agent does not inject notification_service → it is None
        # Should return without raising
        await agent._notify(_USER_ID, _USER_ID, "test message")

    async def test_exception_in_notify_raw_is_swallowed(self):
        from unittest.mock import AsyncMock as _AsyncMock

        notification_service = MagicMock()
        notification_service.notify_raw = _AsyncMock(side_effect=RuntimeError("channel unavailable"))

        port = AsyncMock(spec=AgentNotePort)
        execution_context = MagicMock(spec=AgentExecutionContext)
        execution_context.provider = AsyncMock()
        execution_context.model_name = "test-model"

        agent = NotesAgent(
            config=AgentConfig(
                agent_id=f"notes_agent_{_USER_ID}",
                agent_type="notes",
                timeout_ms=10_000,
                capabilities=["note_management"],
            ),
            execution_context=execution_context,
            notes_port=port,
            notification_service=notification_service,
        )

        # Must not raise despite notify_raw raising
        await agent._notify(_USER_ID, _USER_ID, "test message")
        notification_service.notify_raw.assert_called_once()
