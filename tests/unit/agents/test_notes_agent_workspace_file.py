"""NotesAgent binds workspace_file by code, never through its LLM (AGENT_WORKSPACE_RFC §8.3, D15, B2, B3): validated
before any LLM call (any folder, need not exist), attached to exactly one create/update, announced by a code-appended
system line, rendered in its list and receipts."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.agents.notes_agent import _TOOL_DECLARATIONS, NotesAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.agent_note import AgentNote
from src.domain.workspace import BINDING_INSTRUCTION_NOTE, BINDING_ONE_REMINDER
from src.infrastructure.agent_manifest import Intent
from src.ports.agent_note_port import AgentNotePort
from src.ports.llm_port import AgentExecutionContext, LLMResponse, ToolCall
from src.ports.prompt_builder_port import PromptBuilderPort

USER = "user-abc123"
NOTE_ID = "1729401234567"
DUE = datetime(2030, 3, 10, 9, 0, tzinfo=timezone.utc)
FILE = "projects/morning-briefing.md"
UPDATE = ("update_self_reminder", {"note_id": NOTE_ID, "instruction": "Morning briefing: follow the workspace file."})
CREATE = ("create_self_reminder", {"text": "Morning briefing", "instruction": "Follow the workspace file.",
                                   "due": "2030-03-10T09:00:00+00:00"})
LINE = ("System: workspace_file=projects/morning-briefing.md will be attached by code to the reminder you create or "
        "update in this request. Call update_self_reminder with its note_id even if nothing else changes. Keep the "
        "instruction short and let it refer to the file.")


def _note(**kw):
    return AgentNote(note_id=NOTE_ID, user_id=USER, text="Morning briefing", instruction="Morning briefing",
                     created_at=datetime(2030, 3, 9, tzinfo=timezone.utc), due=DUE, **kw)


def _agent(*, workspace_enabled=True, active=()):
    port = AsyncMock(spec=AgentNotePort)
    port.list_active_notes.return_value = list(active)
    port.create_note.return_value = _note()
    port.update_note.return_value = _note()
    port.delete_note.return_value = True
    execution_context = MagicMock(spec=AgentExecutionContext)
    execution_context.provider = AsyncMock()
    execution_context.model_name = "test-model"
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = ""
    notify = MagicMock()
    notify.notify_raw = AsyncMock()
    agent = NotesAgent(
        config=AgentConfig(agent_id=f"notes_agent_{USER}", agent_type="notes", timeout_ms=10_000,
                           capabilities=["note_management"]),
        execution_context=execution_context, notes_port=port, recurrence=DateutilRecurrenceAdapter(),
        prompt_builder=prompt_builder, notification_service=notify, workspace_enabled=workspace_enabled,
    )
    return agent, port, notify


def _message(query="make the morning reminder follow the briefing file", **fields):
    return AgentMessage.create(
        sender="smart_response_agent", recipient=f"notes_agent_{USER}", intent=AgentIntent.QUERY,
        payload={"intent": Intent.MANAGE_SELF_REMINDERS, "query": query, **fields},
        context={"user_id": USER, "account_id": "acc1"},
    )


def _tools(*calls):
    resp = MagicMock(spec=LLMResponse)
    resp.tool_calls = [ToolCall(name=name, args=dict(args)) for name, args in calls]
    resp.text = ""
    resp.raw_content = None
    return resp


def _text(text="Done."):
    resp = MagicMock(spec=LLMResponse)
    resp.tool_calls = []
    resp.text = text
    resp.raw_content = None
    return resp


def _user_text(agent, index=0):
    return agent._call_llm.await_args_list[index].args[0].messages[0].parts[0].text


class TestBinding:
    async def test_bound_to_the_one_update(self):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        r = await agent.execute(_message(workspace_file="workspace:projects/morning-briefing.md"))
        assert r.status == AgentStatus.SUCCESS
        update = port.update_note.call_args[0][0]
        assert (update.workspace_file, update.clear_workspace_file) == (FILE, False)

    async def test_bound_to_the_one_create(self):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(CREATE), _text()])
        await agent.execute(_message(workspace_file=FILE))
        assert port.create_note.call_args[0][0].workspace_file == FILE

    async def test_the_code_value_overrides_whatever_the_llm_sent(self):
        agent, port, _ = _agent()
        smuggled = ("update_self_reminder", {"note_id": NOTE_ID, "workspace_file": "projects/other.md"})
        agent._call_llm = AsyncMock(side_effect=[_tools(smuggled), _text()])
        await agent.execute(_message(workspace_file=FILE))
        assert port.update_note.call_args[0][0].workspace_file == FILE

    async def test_two_create_update_calls_refuse_all_then_bind_the_next_one(self):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE, CREATE), _tools(UPDATE), _text()])
        r = await agent.execute(_message(workspace_file=FILE))
        assert r.status == AgentStatus.SUCCESS
        port.create_note.assert_not_called()
        port.update_note.assert_called_once()
        assert port.update_note.call_args[0][0].workspace_file == FILE
        second = agent._call_llm.await_args_list[1].args[0]
        assert BINDING_ONE_REMINDER in "".join(p.model_dump_json() for m in second.messages for p in m.parts)

    async def test_the_guard_is_per_execution(self):
        agent, port, _ = _agent()

        async def llm(request, turn=0):
            await asyncio.sleep(0)
            return _text() if not request.tools else _tools(UPDATE)

        agent._call_llm = llm
        bound, plain = await asyncio.gather(agent.execute(_message(workspace_file=FILE)),
                                            agent.execute(_message("rename it")))
        assert bound.status == plain.status == AgentStatus.SUCCESS
        files = sorted((c.args[0].workspace_file or "") for c in port.update_note.call_args_list)
        assert files == ["", FILE]

    async def test_no_create_or_update_says_not_applied(self):
        agent, _, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(("delete_self_reminder", {"note_id": NOTE_ID})),
                                                 _text("Deleted.")])
        r = await agent.execute(_message(workspace_file=FILE))
        assert r.status == AgentStatus.SUCCESS and "workspace_file was not applied" in r.result


class TestSystemLine:
    async def test_appended_when_the_field_is_set(self):
        agent, _, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        await agent.execute(_message(workspace_file=FILE))
        assert _user_text(agent).endswith("\n\n" + LINE)

    async def test_absent_without_the_field(self):
        agent, _, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        await agent.execute(_message())
        assert "System: workspace_file" not in _user_text(agent)

    async def test_fillers_mean_not_provided(self):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        await agent.execute(_message(workspace_file="None"))
        assert "System:" not in _user_text(agent)
        update = port.update_note.call_args[0][0]
        assert update.workspace_file is None and not update.clear_workspace_file

    def test_instruction_descriptions_carry_the_sentence(self):
        for name in ("create_self_reminder", "update_self_reminder"):
            declaration = next(t for t in _TOOL_DECLARATIONS if t["name"] == name)
            properties = declaration["parameters"]["properties"]
            assert BINDING_INSTRUCTION_NOTE in properties["instruction"]["description"]
            assert "workspace_file" not in properties  # never an LLM argument


class TestValidation:
    @pytest.mark.parametrize("raw", ["Morning Briefing", "projects/../x.md", "projects/"])
    async def test_an_invalid_path_is_refused_before_any_llm_call(self, raw):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock()
        r = await agent.execute(_message(workspace_file=raw))
        assert r.status == AgentStatus.FAILED and r.error.startswith("workspace_file: ")
        agent._call_llm.assert_not_awaited()
        port.update_note.assert_not_called()

    @pytest.mark.parametrize("raw", ["scratch/draft.md", "files/lease.pdf", "references/never-written-yet.md"])
    async def test_any_folder_and_a_file_that_does_not_exist_yet(self, raw):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        r = await agent.execute(_message(workspace_file=raw))
        assert r.status == AgentStatus.SUCCESS and port.update_note.call_args[0][0].workspace_file == raw

    async def test_the_field_needs_the_workspace(self):
        agent, _, _ = _agent(workspace_enabled=False)
        agent._call_llm = AsyncMock()
        r = await agent.execute(_message(workspace_file=FILE))
        assert r.status == AgentStatus.FAILED and "needs the workspace" in r.error

    async def test_remove_works_without_the_workspace(self):
        agent, port, _ = _agent(workspace_enabled=False)
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        r = await agent.execute(_message(workspace_file="remove"))
        assert r.status == AgentStatus.SUCCESS and port.update_note.call_args[0][0].clear_workspace_file


class TestClearAndRender:
    async def test_remove_clears_and_the_receipt_says_so(self):
        agent, port, notify = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        await agent.execute(_message(workspace_file="REMOVE"))
        update = port.update_note.call_args[0][0]
        assert update.clear_workspace_file and update.workspace_file is None
        assert "System: the workspace file will be removed by code" in _user_text(agent)
        assert notify.notify_raw.call_args.kwargs["text"].endswith(" · workspace file removed")

    async def test_the_update_receipt_names_the_file(self):
        agent, port, notify = _agent()
        port.update_note.return_value = _note(workspace_file=FILE)
        agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
        await agent.execute(_message(workspace_file=FILE))
        assert notify.notify_raw.call_args.kwargs["text"].endswith(f" · workspace file {FILE}")

    async def test_the_create_receipt(self):
        agent, _, notify = _agent()
        agent._call_llm = AsyncMock(side_effect=[_tools(CREATE), _text()])
        await agent.execute(_message(workspace_file=FILE))
        receipt = notify.notify_raw.call_args.kwargs["text"]
        assert receipt.startswith("📌 Reminder set:") and receipt.endswith(f" · workspace file {FILE}")

    async def test_the_listing_shows_the_file(self):
        agent, _, _ = _agent(active=[_note(workspace_file=FILE)])
        agent._call_llm = AsyncMock(side_effect=[_text("You have one reminder.")])
        await agent.execute(_message("list my reminders"))
        system = agent._call_llm.await_args_list[0].args[0].system_instruction
        assert f"    instruction: Morning briefing\n    workspace file: workspace:{FILE}" in system


class TestBindingRefusalExhaustsTurns:
    async def test_refusals_until_max_turns_return_the_one_reminder_error(self):
        agent, port, _ = _agent()
        agent._call_llm = AsyncMock(side_effect=lambda *a, **k: _tools(UPDATE, CREATE))
        r = await agent.execute(_message(workspace_file=FILE))
        assert r.status == AgentStatus.FAILED
        assert BINDING_ONE_REMINDER in str(r.error)
        port.create_note.assert_not_called()
        port.update_note.assert_not_called()

    async def test_a_co_emitted_delete_with_two_create_update_runs_nothing(self):
        agent, port, _ = _agent()
        delete = ("delete_self_reminder", {"note_id": NOTE_ID})
        agent._call_llm = AsyncMock(side_effect=[_tools(delete, UPDATE, CREATE), _text("stop")])
        await agent.execute(_message(workspace_file=FILE))
        port.delete_note.assert_not_called()
        port.create_note.assert_not_called()
        port.update_note.assert_not_called()
