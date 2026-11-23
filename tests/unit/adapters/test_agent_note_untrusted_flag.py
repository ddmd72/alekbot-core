"""The reminder's `untrusted` flag end to end (EXTERNAL_TASK_QUEUE_RFC §6.1): a note the reminders specialist writes in
a tainted execution is stored flagged and read back flagged; an update flags and never clears; an old record reads
as unflagged; the specialist reads the taint from the delegation context. The store kit's Firestore fake and port."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from src.domain.agent_note import NoteUpdate
from src.domain.execution_taint import EXECUTION_TAINTED_KEY
from tests.unit.adapters.test_agent_note_store_kit import DUE, create, notes  # noqa: F401
from tests.unit.agents.test_notes_agent_workspace_file import CREATE, UPDATE, _agent, _message, _text, _tools


async def test_a_flagged_create_is_stored_flagged_and_read_back_flagged(notes):  # noqa: F811
    note = await create(notes, untrusted=True)
    assert notes.docs[note.note_id]["untrusted"] is True and note.untrusted is True
    [listed] = await notes.port.list_active_notes("u1", as_of=DUE - timedelta(days=2))
    assert listed.untrusted is True


async def test_an_unflagged_create_writes_no_field_and_an_old_record_reads_unflagged(notes):  # noqa: F811
    note = await create(notes)
    assert "untrusted" not in notes.docs[note.note_id] and note.untrusted is False
    assert (await notes.port.get_note("u1", note.note_id)).untrusted is False


async def test_an_update_flags_and_an_unflagged_update_never_clears(notes):  # noqa: F811
    note = await create(notes)
    flagged = await notes.port.update_note(NoteUpdate(note_id=note.note_id, user_id="u1", instruction="new", untrusted=True))
    assert flagged.untrusted is True
    again = await notes.port.update_note(NoteUpdate(note_id=note.note_id, user_id="u1", text="renamed"))
    assert again.untrusted is True and notes.docs[note.note_id]["untrusted"] is True


@pytest.mark.parametrize("context,expected", [({}, False), ({EXECUTION_TAINTED_KEY: False}, False),
                                              ({EXECUTION_TAINTED_KEY: True}, True)])
async def test_the_specialist_flags_what_it_writes_from_the_delegation_context(context, expected):
    agent, port, _ = _agent()
    agent._call_llm = AsyncMock(side_effect=[_tools(CREATE), _text()])
    message = _message()
    message.context.update(context)
    await agent.execute(message)
    assert port.create_note.call_args[0][0].untrusted is expected

    agent, port, _ = _agent()
    agent._call_llm = AsyncMock(side_effect=[_tools(UPDATE), _text()])
    message = _message()
    message.context.update(context)
    await agent.execute(message)
    assert port.update_note.call_args[0][0].untrusted is expected
