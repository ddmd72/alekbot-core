"""Mutation-survivor tests for NotesAgent (group na2).

An update that sets a recurrence rule without a new `due` snaps the stored due onto the rule,
so `_current_due` must read the right note, scoped by the right user.
"""

from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.agents.notes_agent import NotesAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.agent_note import AgentNote, NoteUpdate
from src.infrastructure.agent_manifest import Intent
from src.ports.agent_note_port import AgentNotePort
from src.ports.llm_port import AgentExecutionContext, LLMResponse, ToolCall
from src.ports.prompt_builder_port import PromptBuilderPort

_USER = "alpha-user"
_OTHER_USER = "beta-user"
_STORED_DUE = datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)
_OTHER_DUE = datetime(2027, 1, 5, 9, 0, tzinfo=timezone.utc)


def _note(user_id: str, note_id: str, due: datetime) -> AgentNote:
    return AgentNote(
        note_id=note_id, user_id=user_id, text="t", instruction="t",
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc), due=due,
    )


class _FakeNotes(AsyncMock):
    """In-memory port with real ownership semantics."""

    def __init__(self) -> None:
        super().__init__(spec=AgentNotePort)
        self.notes: Dict[Tuple[str, str], AgentNote] = {}
        self.updates: list = []
        self.get_note = self._get_note
        self.update_note = self._update_note

    async def _get_note(self, user_id: str, note_id: str) -> Optional[AgentNote]:
        return self.notes.get((user_id, note_id))

    async def _update_note(self, data: NoteUpdate) -> AgentNote:
        self.updates.append(data)
        return self.notes[(data.user_id, data.note_id)]


def _agent(port: _FakeNotes) -> NotesAgent:
    ctx = MagicMock(spec=AgentExecutionContext)
    ctx.provider = AsyncMock()
    ctx.model_name = "test-model"
    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = ""
    return NotesAgent(
        config=AgentConfig(agent_id=f"notes_agent_{_USER}", agent_type="notes", timeout_ms=10_000,
                           capabilities=["note_management"]),
        execution_context=ctx, notes_port=port, recurrence=DateutilRecurrenceAdapter(),
        prompt_builder=prompt_builder,
    )


def _msg() -> AgentMessage:
    return AgentMessage.create(
        sender="quick_response_agent", recipient=f"notes_agent_{_USER}", intent=AgentIntent.QUERY,
        payload={"intent": Intent.MANAGE_SELF_REMINDERS, "query": "make it daily"},
        context={"user_id": _USER},
    )


async def _update(agent: NotesAgent, args: dict):
    resp = MagicMock(spec=LLMResponse)
    resp.tool_calls = [ToolCall(name="update_self_reminder", args=args, id="tc-1")]
    resp.text = ""
    resp.raw_content = None
    with patch.object(agent, "_call_llm", return_value=resp):
        return await agent.execute(_msg())


async def test_recurrence_update_snaps_onto_the_stored_due_of_that_users_note():
    port = _FakeNotes()
    port.notes[(_USER, "note-1")] = _note(_USER, "note-1", _STORED_DUE)
    port.notes[(_USER, "note-2")] = _note(_USER, "note-2", _OTHER_DUE)
    port.notes[(_OTHER_USER, "note-1")] = _note(_OTHER_USER, "note-1", _OTHER_DUE)
    agent = _agent(port)

    response = await _update(agent, {"note_id": "note-1", "recurrence": "FREQ=DAILY"})

    assert response.status == AgentStatus.SUCCESS
    assert len(port.updates) == 1
    snapped = port.updates[0].due
    assert snapped is not None and snapped.astimezone(timezone.utc).date() == _STORED_DUE.date()


async def test_recurrence_update_of_a_note_owned_by_someone_else_is_not_found():
    port = _FakeNotes()
    port.notes[(_OTHER_USER, "note-1")] = _note(_OTHER_USER, "note-1", _STORED_DUE)
    agent = _agent(port)

    await _update(agent, {"note_id": "note-1", "recurrence": "FREQ=DAILY"})

    assert port.updates == []
