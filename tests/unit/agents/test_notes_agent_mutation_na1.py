"""Mutation survivors of notes_agent (group na1): prompt-builder scoping and the update-path recurrence error."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.agents.notes_agent import NotesAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.agent_note import AgentNote
from src.infrastructure.agent_manifest import Intent
from src.ports.agent_note_port import AgentNotePort
from src.ports.llm_port import AgentExecutionContext, LLMResponse, ToolCall
from src.ports.prompt_builder_port import PromptBuilderPort

USER = "alpha-user"
ACCOUNT = "acct-1"
NOTE_ID = "note-1"


def _note():
    return AgentNote(note_id=NOTE_ID, user_id=USER, text="Water plants", instruction="Water plants",
                     created_at=datetime(2030, 3, 9, tzinfo=timezone.utc),
                     due=datetime(2030, 3, 10, 9, 0, tzinfo=timezone.utc))


def _agent():
    port = AsyncMock(spec=AgentNotePort)
    port.list_active_notes.return_value = []
    port.create_note.return_value = _note()
    port.update_note.return_value = _note()
    ctx = MagicMock(spec=AgentExecutionContext)
    ctx.provider = AsyncMock()
    ctx.model_name = "test-model"
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = ""
    notify = MagicMock()
    notify.notify_raw = AsyncMock()
    agent = NotesAgent(
        config=AgentConfig(agent_id=f"notes_agent_{USER}", agent_type="notes", timeout_ms=10_000,
                           capabilities=["note_management"]),
        execution_context=ctx, notes_port=port, recurrence=DateutilRecurrenceAdapter(),
        prompt_builder=pb, notification_service=notify,
    )
    return agent, port, pb


def _message():
    return AgentMessage.create(
        sender="smart_response_agent", recipient=f"notes_agent_{USER}", intent=AgentIntent.QUERY,
        payload={"intent": Intent.MANAGE_SELF_REMINDERS, "query": "change the reminder"},
        context={"user_id": USER, "account_id": ACCOUNT},
    )


def _tools(name, args):
    r = MagicMock(spec=LLMResponse)
    r.tool_calls = [ToolCall(name=name, args=dict(args))]
    r.text = ""
    r.raw_content = None
    return r


def _text():
    r = MagicMock(spec=LLMResponse)
    r.tool_calls = []
    r.text = "Done."
    r.raw_content = None
    return r


async def test_prompt_is_built_for_this_user_and_account():
    agent, _, pb = _agent()
    agent._call_llm = AsyncMock(side_effect=[_text()])
    r = await agent.execute(_message())
    assert r.status == AgentStatus.SUCCESS
    kwargs = pb.build_for_agent.call_args.kwargs
    assert kwargs["agent_type"] == "notes"
    assert kwargs["user_id"] == USER
    assert kwargs["account_id"] == ACCOUNT


async def test_update_with_invalid_recurrence_returns_the_error_to_the_llm_and_stores_nothing():
    agent, port, _ = _agent()
    bad = {"note_id": NOTE_ID, "recurrence": "definitely not a rule"}
    agent._call_llm = AsyncMock(side_effect=[_tools("update_self_reminder", bad), _text()])
    r = await agent.execute(_message())
    assert r.status == AgentStatus.FAILED
    assert r.error
    port.update_note.assert_not_called()
